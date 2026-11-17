"""Live activity snapshot and a shared event ring.

The worker and the web process are separate. Events go in SQLite. Meters and
per-channel flow are written by the worker into logs/audio_status.json, which
this module reads back.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from soundcore.audio.sanketptt import load_sanket_status
from soundcore.audio.service import load_audio_status
from soundcore.models import (
    ActivityEvent,
    Channel,
    Recording,
    Route,
    S3Object,
    StorageState,
    Transcription,
    TranscriptionStatus,
    utcnow,
)
from soundcore.ops import channel_endpoint
from soundcore.settings_store import load_runtime
from soundcore.storage.guard import measure_disk

logger = logging.getLogger(__name__)

MAX_EVENTS = 1000


def record_activity(kind: str, message: str, *, channel: str = "", level: str = "info", session=None) -> None:
    """Append one event and drop rows older than the newest 1000."""

    def _write(db) -> None:
        db.add(
            ActivityEvent(
                created_at=utcnow(),
                level=level,
                kind=kind,
                channel=(channel or "")[:80],
                message=message,
            )
        )
        db.flush()
        border = (
            db.query(ActivityEvent.id).order_by(ActivityEvent.id.desc()).offset(MAX_EVENTS - 1).limit(1).first()
        )
        if border is not None:
            db.query(ActivityEvent).filter(ActivityEvent.id < border[0]).delete(synchronize_session=False)
            db.flush()

    try:
        if session is not None:
            _write(session)
            return
        from soundcore.db import session_scope

        with session_scope() as db:
            _write(db)
    except Exception:
        logger.exception("activity event was not stored")


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def list_events(session, *, kind: str = "", channel: str = "", limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit or 200), MAX_EVENTS))
    query = session.query(ActivityEvent)
    if kind:
        query = query.filter(ActivityEvent.kind == kind)
    if channel:
        query = query.filter(ActivityEvent.channel == channel)
    rows = query.order_by(ActivityEvent.id.desc()).limit(limit).all()
    return [
        {
            "id": row.id,
            "created_at": _iso(row.created_at),
            "level": row.level,
            "kind": row.kind,
            "channel": row.channel,
            "message": row.message,
        }
        for row in rows
    ]


def _device_index(devices) -> dict[str, object]:
    return {device.id: device for device in devices}


def build_activity(session, settings, backend=None, *, kind: str = "", channel: str = "", limit: int = 200) -> dict:
    devices = []
    scan_error = ""
    if backend is not None:
        try:
            devices = list(backend.list_devices())
        except Exception as exc:
            scan_error = str(exc)
    by_id = _device_index(devices)
    live = load_audio_status(settings.data_dir)
    flow = live.get("flow") if isinstance(live.get("flow"), dict) else {}
    notices = {item.get("output_channel_id"): item for item in live.get("notices") or [] if isinstance(item, dict)}
    sanket = load_sanket_status(settings.data_dir)
    runtime = load_runtime(session, settings)
    usage = measure_disk(settings.data_dir)

    channels_out = []
    for row in session.query(Channel).order_by(Channel.direction, Channel.id).all():
        device_id, backend_name = channel_endpoint(row)
        found = by_id.get(device_id)
        stats = flow.get(str(row.id)) or {}
        available = bool(found.available) if found is not None else False
        if backend_name == "sanketptt":
            available = bool(sanket.get("connected"))
        status = stats.get("status") or ("idle" if row.enabled else "off")
        if stats.get("error"):
            status = "error"
        if row.direction == "out":
            note = notices.get(row.id) or {}
            if note.get("reason") == "disconnected" or (row.enabled and not available):
                status = "disconnected" if not stats.get("status") else status
            if stats.get("status") == "playing":
                status = "playing"
        channels_out.append(
            {
                "id": row.id,
                "name": row.name,
                "slug": row.slug,
                "direction": row.direction,
                "backend": backend_name,
                "kind": row.device_kind,
                "device_id": device_id,
                "enabled": bool(row.enabled),
                "available": available,
                "status": status,
                "level": float(stats.get("level") or 0),
                "last_audio": stats.get("last_audio") or "",
                "bytes_per_sec": float(stats.get("bytes_per_sec") or 0),
                "packets_per_sec": float(stats.get("packets_per_sec") or 0),
                "recording_file": stats.get("recording_file") or "",
                "recording_started": stats.get("recording_started") or "",
                "error": stats.get("error") or "",
            }
        )

    by_channel = {item["id"]: item for item in channels_out}
    routes_out = []
    for route in session.query(Route).order_by(Route.id).all():
        src = by_channel.get(route.input_channel_id)
        dst = by_channel.get(route.output_channel_id)
        if src is None or dst is None:
            continue
        if not route.enabled:
            state = "idle"
        elif not dst["available"]:
            state = "disconnected"
        elif src["status"] in {"capturing", "receiving", "playing"} and src["bytes_per_sec"] > 0:
            state = "flowing"
        else:
            state = "idle"
        routes_out.append(
            {
                "id": route.id,
                "enabled": bool(route.enabled),
                "input_id": src["id"],
                "output_id": dst["id"],
                "input": src["name"],
                "output": dst["name"],
                "input_backend": src["backend"],
                "output_backend": dst["backend"],
                "state": state,
                "bytes_per_sec": src["bytes_per_sec"],
                "packets_per_sec": src["packets_per_sec"],
                "recording_file": src["recording_file"],
                "recording_started": src["recording_started"],
            }
        )

    discovered = []
    for device in devices:
        discovered.append(
            {
                "id": device.id,
                "name": device.name,
                "direction": device.direction,
                "backend": device.backend,
                "kind": device.kind,
                "available": bool(device.available),
            }
        )

    last_upload = session.query(S3Object).order_by(S3Object.id.desc()).first()
    queued = (
        session.query(Transcription)
        .filter(Transcription.status.in_([TranscriptionStatus.PENDING, TranscriptionStatus.RETRYING, TranscriptionStatus.RUNNING]))
        .count()
    )
    last_tx = (
        session.query(Transcription)
        .filter(Transcription.status == TranscriptionStatus.DONE)
        .order_by(Transcription.id.desc())
        .first()
    )
    local_waiting = (
        session.query(Recording)
        .filter(Recording.storage_state.in_([StorageState.LOCAL_ONLY, StorageState.UPLOADED_UNVERIFIED, StorageState.WRITING]))
        .count()
    )
    pw_error = ""
    pipewire = getattr(backend, "pipewire", None)
    if pipewire is not None:
        pw_error = getattr(pipewire, "last_error", "") or ""
    bluetooth = [device.name for device in devices if device.kind == "bluetooth" and device.available]
    pipewire_up = any(device.backend == "pipewire" and device.available for device in devices) and not pw_error

    return {
        "channels": channels_out,
        "routes": routes_out,
        "discovered": discovered,
        "connections": {
            "sanket": {
                "connected": bool(sanket.get("connected")),
                "broker": sanket.get("broker") or sanket.get("broker_url") or "",
                "last_error": sanket.get("last_error") or "",
                "subscribed": sanket.get("subscribed") or [],
                "talkers": [
                    {"channel": name, "talking": row.get("talking") or ""}
                    for name, row in (sanket.get("channels") or {}).items()
                    if isinstance(row, dict) and row.get("talking")
                ],
            },
            "pipewire": {"reachable": pipewire_up, "error": pw_error or scan_error},
            "bluetooth": {"connected": bool(bluetooth), "devices": bluetooth},
            "s3": {
                "configured": bool(settings.s3_configured),
                "waiting": local_waiting,
                "last_upload": _iso(last_upload.uploaded_at) if last_upload is not None else "",
                "last_key": last_upload.key if last_upload is not None else "",
            },
            "transcription": {
                "queued": queued,
                "last_text": (last_tx.text or "")[:180] if last_tx is not None else "",
                "last_at": _iso(last_tx.completed_at) if last_tx is not None else "",
            },
            "disk": {
                "used_ratio": round(usage.used_ratio, 4),
                "free_ratio": round(usage.free_ratio, 4),
                "free_bytes": usage.free,
                "total_bytes": usage.total,
            },
            "stream_cap": runtime.stream_cap,
        },
        "events": list_events(session, kind=kind, channel=channel, limit=limit),
    }
