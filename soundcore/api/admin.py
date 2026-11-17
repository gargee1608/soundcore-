from flask import Blueprint, current_app, g, jsonify, request

from soundcore.auth import login_required
from soundcore.settings_store import load_runtime, set_setting

bp = Blueprint("admin_api", __name__)


@bp.get("/api/admin/settings")
@login_required
def get_settings():
    settings = current_app.config["SETTINGS"]
    runtime = load_runtime(g.db, settings)
    return jsonify(
        {
            "stream_cap": runtime.stream_cap,
            "warn_ratio": runtime.warn_ratio,
            "chunk_seconds": runtime.chunk_seconds,
            "idle_days": runtime.idle_days,
            "timezone": runtime.timezone,
            "transcribe_enabled": runtime.transcribe_enabled,
            "boondock_language": runtime.boondock_language,
            "notify_email": runtime.notify_email,
            "notify_webhook": runtime.notify_webhook,
            "secrets": {
                "admin_password": bool(settings.admin_password),
                "boondock_api_key": settings.boondock_configured,
                "s3": settings.s3_configured,
                "smtp": bool(settings.smtp_host and settings.smtp_from),
            },
        }
    )


@bp.post("/api/admin/settings")
@login_required
def update_settings():
    payload = request.get_json(silent=True) or {}
    try:
        cap = int(payload.get("stream_cap"))
        chunk = int(payload.get("chunk_seconds"))
        idle = int(payload.get("idle_days"))
    except (TypeError, ValueError):
        return jsonify({"error": "Stream cap, chunk length, and idle days must be numbers."}), 400
    if cap < 1 or cap > 64:
        return jsonify({"error": "Stream cap must be between 1 and 64."}), 400
    if chunk < 5 or chunk > 3600:
        return jsonify({"error": "Chunk length must be between 5 and 3600 seconds."}), 400
    if idle < 1 or idle > 3650:
        return jsonify({"error": "Idle days must be between 1 and 3650."}), 400
    set_setting(g.db, "stream_cap", str(cap))
    set_setting(g.db, "chunk_seconds", str(chunk))
    set_setting(g.db, "idle_days", str(idle))
    set_setting(g.db, "transcribe_enabled", "1" if payload.get("transcribe_enabled") else "0")
    set_setting(g.db, "boondock_language", str(payload.get("boondock_language") or "")[:16])
    set_setting(g.db, "notify_email", str(payload.get("notify_email") or "")[:200])
    set_setting(g.db, "notify_webhook", str(payload.get("notify_webhook") or "")[:500])
    if payload.get("timezone"):
        set_setting(g.db, "timezone", str(payload["timezone"])[:64])
    return jsonify({"ok": True})
