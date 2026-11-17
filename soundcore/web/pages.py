from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from soundcore.activity import build_activity
from soundcore.audio.detect import AudioBackendError
from soundcore.audio.sanketptt import device_id_for, load_sanket_status, parse_sanket_source, web_url
from soundcore.audio.service import load_audio_status
from soundcore.auth import authenticate, login_required, login_user, logout_user
from soundcore.maintenance import run_guard
from soundcore.models import Channel, Event, Recording, Route, Setting, Transcription, utcnow
from soundcore.ops import OpsError, active_inputs, delete_channel, save_channel, save_route
from soundcore.settings_store import load_runtime, set_setting
from soundcore.transcription.worker import enqueue_transcription
from soundcore.settings_store import load_runtime, set_setting
from soundcore.status import console_status
from soundcore.transfer import build_export_zip, import_zip
import io

bp = Blueprint("pages", __name__)


@bp.get("/login")
def login():
    if g.get("current_user"):
        return redirect(url_for("pages.dashboard"))
    return render_template("login.html")


@bp.post("/login")
def login_post():
    user = authenticate(g.db, request.form.get("email", ""), request.form.get("password", ""))
    if user is None:
        flash("That email or password does not match.", "error")
        return render_template("login.html"), 401
    login_user(user)
    nxt = request.form.get("next") or ""
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("pages.dashboard")
    return redirect(nxt)


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("pages.login"))


@bp.get("/")
@login_required
def dashboard():
    status = console_status(g.db, current_app.config["SETTINGS"], current_app.extensions.get("audio"))
    recent = g.db.query(Recording).order_by(Recording.started_at.desc()).limit(6).all()
    names = {row.id: row.name for row in g.db.query(Channel).all()}
    return render_template("dashboard.html", status=status, recent=recent, names=names)


def _scan_devices(backend):
    try:
        devices = list(backend.list_devices())
        error = getattr(backend, "last_error", "") or getattr(getattr(backend, "pipewire", None), "last_error", "")
    except AudioBackendError as exc:
        devices = []
        error = str(exc)
    return devices, error


def _audio_panel(backend, settings, channels):
    devices, error = _scan_devices(backend)
    known = set()
    for row in channels:
        known.add(row.alsa_device)
        if getattr(row, "device_id", ""):
            known.add(row.device_id)
    fresh = [device for device in devices if device.id not in known and device.backend != "simulated"]
    snapshot = load_audio_status(settings.data_dir)
    sinks = [device for device in devices if device.backend == "pipewire" and device.direction == "out"]
    sources = [device for device in devices if device.backend == "pipewire" and device.direction == "in"]
    bluetooth = [device for device in sinks if device.kind == "bluetooth"]
    available = {device.id for device in devices if device.available}
    return {
        "devices": devices,
        "fresh": fresh,
        "error": error,
        "snapshot": snapshot,
        "sinks": sinks,
        "sources": sources,
        "bluetooth": bluetooth,
        "bluetooth_connected": bool(bluetooth),
        "available": available,
        "input_backends": sorted({device.backend for device in devices if device.direction == "in"}),
        "output_backends": sorted({device.backend for device in devices if device.direction == "out"}),
    }


@bp.get("/activity")
@login_required
def activity_page():
    settings = current_app.config["SETTINGS"]
    payload = build_activity(g.db, settings, current_app.extensions.get("audio_backend"))
    return render_template("activity.html", initial=payload)


@bp.get("/channels")
@login_required
def channels():
    backend = current_app.extensions["audio_backend"]
    settings = current_app.config["SETTINGS"]
    rows = g.db.query(Channel).order_by(Channel.direction, Channel.id).all()
    panel = _audio_panel(backend, settings, rows)
    sanket = load_sanket_status(settings.data_dir)
    sanket.setdefault("channels", {})
    sanket.setdefault("connected", False)
    sanket.setdefault("last_error", "")
    sanket.setdefault("broker", "")
    if sanket.get("connected"):
        for name in sanket.get("subscribed") or []:
            panel["available"].add(device_id_for(name))
    runtime = load_runtime(g.db, settings)
    active = active_inputs(g.db)
    return render_template(
        "channels.html",
        channels=rows,
        fresh=panel["fresh"],
        rates=settings.allowed_sample_rates,
        audio=panel,
        sanket=sanket,
        sanket_ids=[row.device_id for row in rows if (row.audio_backend or "") == "sanketptt"],
        sanket_discover=_flag(g.db, "sanket_discover"),
        sanket_auto_add=_flag(g.db, "sanket_auto_add"),
        active_inputs=active,
        stream_cap=runtime.stream_cap,
        web_url=web_url,
    )


def _flag(session, key: str) -> bool:
    row = session.get(Setting, key)
    return bool(row and str(row.value).strip().lower() in {"1", "true", "yes", "on"})


@bp.post("/channels/sanket")
@login_required
def channels_sanket_add():
    settings = current_app.config["SETTINGS"]
    try:
        source = parse_sanket_source(request.form.get("source", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("pages.channels"))
    try:
        save_channel(
            g.db,
            settings,
            channel_id=None,
            name=request.form.get("name") or source.channel,
            alsa_device=device_id_for(source.channel),
            device_id=device_id_for(source.channel),
            audio_backend="sanketptt",
            sample_rate=int(request.form.get("sample_rate") or settings.default_sample_rate),
            gain_db=0.0,
            enabled=request.form.get("enabled") == "on",
            direction="in",
            device_kind="radio",
            actor=g.current_user.email,
        )
        flash(f"SanketPTT channel {source.channel} added.", "ok")
    except (OpsError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.channels"))


@bp.post("/channels/sanket/discover")
@login_required
def channels_sanket_discover():
    set_setting(g.db, "sanket_discover", "1" if request.form.get("enabled") == "on" else "0")
    if request.form.get("enabled") == "on":
        flash("Discovery is on. Active SanketPTT channels show up here as people join them.", "ok")
    else:
        flash("SanketPTT discovery is off.", "ok")
    return redirect(url_for("pages.channels"))


@bp.post("/channels/sanket/auto-add")
@login_required
def channels_sanket_auto_add():
    set_setting(g.db, "sanket_auto_add", "1" if request.form.get("enabled") == "on" else "0")
    flash("Auto-add is on. New channels are saved turned off so the stream cap still applies." if request.form.get("enabled") == "on" else "Auto-add is off.", "ok")
    return redirect(url_for("pages.channels"))


@bp.post("/channels/rescan")
@login_required
def channels_rescan():
    backend = current_app.extensions["audio_backend"]
    if hasattr(backend, "rescan"):
        backend.rescan()
    flash("Device scan refreshed.", "ok")
    return redirect(url_for("pages.channels"))


@bp.post("/channels")
@login_required
def channels_save():
    settings = current_app.config["SETTINGS"]
    channel_id = request.form.get("channel_id")
    try:
        save_channel(
            g.db,
            settings,
            channel_id=int(channel_id) if channel_id else None,
            name=request.form.get("name", ""),
            alsa_device=request.form.get("alsa_device", ""),
            sample_rate=int(request.form.get("sample_rate") or settings.default_sample_rate),
            gain_db=float(request.form.get("gain_db") or 0),
            enabled=request.form.get("enabled") == "on",
            direction=request.form.get("direction") or "in",
            device_kind=request.form.get("device_kind") or "usb",
            usb_id=request.form.get("usb_id") or None,
            audio_backend=request.form.get("audio_backend") or "",
            device_id=request.form.get("alsa_device") or request.form.get("device_id") or "",
            actor=g.current_user.email,
        )
        flash("Channel saved.", "ok")
    except (OpsError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.channels"))


@bp.post("/channels/<int:channel_id>/delete")
@login_required
def channels_delete(channel_id: int):
    try:
        result = delete_channel(g.db, channel_id)
    except OpsError as exc:
        flash(str(exc), "error")
        return redirect(url_for("pages.channels"))
    if result == "disabled":
        flash("This channel has recordings, so it was disabled instead of deleted.", "ok")
    else:
        flash("Channel removed.", "ok")
    return redirect(url_for("pages.channels"))


@bp.get("/routing")
@login_required
def routing():
    inputs = g.db.query(Channel).filter_by(direction="in").order_by(Channel.name).all()
    outputs = g.db.query(Channel).filter_by(direction="out").order_by(Channel.name).all()
    routes = g.db.query(Route).order_by(Route.id).all()
    names = {row.id: row for row in inputs + outputs}
    backend = current_app.extensions["audio_backend"]
    settings = current_app.config["SETTINGS"]
    panel = _audio_panel(backend, settings, inputs + outputs)
    return render_template(
        "routing.html",
        inputs=inputs,
        outputs=outputs,
        routes=routes,
        names=names,
        audio=panel,
    )


@bp.post("/routing")
@login_required
def routing_save():
    try:
        save_route(
            g.db,
            int(request.form["input_channel_id"]),
            int(request.form["output_channel_id"]),
            enabled=request.form.get("enabled", "on") == "on",
        )
        flash("Route saved. Audio keeps recording while it is relayed.", "ok")
    except (OpsError, KeyError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.routing"))


@bp.post("/routing/<int:route_id>/delete")
@login_required
def routing_delete(route_id: int):
    route = g.db.get(Route, route_id)
    if route is not None:
        g.db.delete(route)
        flash("Route removed.", "ok")
    return redirect(url_for("pages.routing"))


@bp.get("/recordings")
@login_required
def recordings():
    channel_id = request.args.get("channel_id", type=int)
    query = g.db.query(Recording).order_by(Recording.started_at.desc())
    if channel_id:
        query = query.filter(Recording.channel_id == channel_id)
    rows = query.limit(100).all()
    channels = g.db.query(Channel).filter_by(direction="in").order_by(Channel.name).all()
    names = {row.id: row.name for row in channels}
    ids = [row.id for row in rows] or [0]
    transcripts = {
        row.recording_id: row for row in g.db.query(Transcription).filter(Transcription.recording_id.in_(ids)).all()
    }
    return render_template(
        "recordings.html",
        recordings=rows,
        channels=channels,
        names=names,
        transcripts=transcripts,
        channel_id=channel_id,
    )


@bp.post("/recordings/<int:recording_id>/transcribe")
@login_required
def queue_transcription(recording_id: int):
    recording = g.db.get(Recording, recording_id)
    if recording is None:
        flash("That recording is gone.", "error")
    elif recording.storage_state == "writing":
        flash("This chunk is still open. Transcription starts after it closes.", "error")
    else:
        enqueue_transcription(g.db, recording)
        flash("Transcription queued.", "ok")
    return redirect(url_for("pages.recordings"))


@bp.get("/storage")
@login_required
def storage():
    status = console_status(g.db, current_app.config["SETTINGS"], current_app.extensions.get("audio"))
    events = g.db.query(Event).order_by(Event.id.desc()).limit(40).all()
    return render_template("storage.html", status=status, events=events)


@bp.post("/storage/check")
@login_required
def storage_check():
    summary = run_guard(current_app.config["SETTINGS"])
    if summary.get("triggered"):
        flash(
            f"Storage check ran. Removed {summary['deleted']} verified file(s); kept {summary['kept']} that could not be verified.",
            "ok",
        )
    else:
        flash("Storage is under the cleanup line. Nothing was deleted.", "ok")
    return redirect(url_for("pages.storage"))


@bp.post("/storage/events/<int:event_id>/ack")
@login_required
def storage_ack(event_id: int):
    event = g.db.get(Event, event_id)
    if event is not None:
        event.acked_at = utcnow()
    return redirect(url_for("pages.storage"))


@bp.get("/settings")
@login_required
def settings_page():
    settings = current_app.config["SETTINGS"]
    runtime = load_runtime(g.db, settings)
    return render_template(
        "settings.html",
        runtime=runtime,
        secrets={
            "boondock": settings.boondock_configured,
            "s3": settings.s3_configured,
            "smtp": bool(settings.smtp_host and settings.smtp_from),
            "bucket": settings.s3_bucket,
            "boondock_base": settings.boondock_base_url,
        },
    )


@bp.post("/settings")
@login_required
def settings_save():
    try:
        cap = int(request.form["stream_cap"])
        chunk = int(request.form["chunk_seconds"])
        idle = int(request.form["idle_days"])
    except (KeyError, ValueError):
        flash("Check the numbers and try again.", "error")
        return redirect(url_for("pages.settings_page"))
    if not (1 <= cap <= 64 and 5 <= chunk <= 3600 and 1 <= idle <= 3650):
        flash("Cap must be 1–64, chunks 5–3600 seconds, idle days 1–3650.", "error")
        return redirect(url_for("pages.settings_page"))
    set_setting(g.db, "stream_cap", str(cap))
    set_setting(g.db, "chunk_seconds", str(chunk))
    set_setting(g.db, "idle_days", str(idle))
    set_setting(g.db, "transcribe_enabled", "1" if request.form.get("transcribe_enabled") == "on" else "0")
    set_setting(g.db, "boondock_language", (request.form.get("boondock_language") or "")[:16])
    set_setting(g.db, "notify_email", (request.form.get("notify_email") or "")[:200])
    set_setting(g.db, "notify_webhook", (request.form.get("notify_webhook") or "")[:500])
    tz = (request.form.get("timezone") or "").strip()
    if tz:
        set_setting(g.db, "timezone", tz[:64])
    flash("Settings saved. The midnight job uses the timezone from the next process start.", "ok")
    return redirect(url_for("pages.settings_page"))


@bp.get("/export")
@login_required
def export_page():
    settings = current_app.config["SETTINGS"]
    payload = build_export_zip(g.db, settings.data_dir)
    return send_file(
        io.BytesIO(payload),
        mimetype="application/zip",
        as_attachment=True,
        download_name="soundcore-export.zip",
    )


@bp.post("/import")
@login_required
def import_page():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Choose an export zip first.", "error")
        return redirect(url_for("pages.settings_page"))
    try:
        summary = import_zip(g.db, upload.read(), current_app.config["SETTINGS"].data_dir)
    except Exception as exc:
        g.db.rollback()
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("pages.settings_page"))
    flash(
        f"Restored {summary['channels']} channels, {summary['recordings']} recordings, "
        f"{summary['transcriptions']} transcripts, {summary['files_restored']} audio files.",
        "ok",
    )
    return redirect(url_for("pages.dashboard"))
