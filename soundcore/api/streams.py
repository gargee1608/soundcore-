from flask import Blueprint, current_app, g, jsonify

from soundcore.audio.sanketptt import load_sanket_status, probe_connection, redact_broker, resolve_brokers
from soundcore.auth import login_required
from soundcore.capacity import evaluate_capacity
from soundcore.models import Channel, Stream
from soundcore.ops import active_inputs, channel_endpoint
from soundcore.settings_store import load_runtime

bp = Blueprint("streams_api", __name__)


@bp.get("/api/streams")
@login_required
def list_streams():
    runtime = load_runtime(g.db, current_app.config["SETTINGS"])
    active = active_inputs(g.db)
    decision = evaluate_capacity(active, runtime.stream_cap, warn_ratio=runtime.warn_ratio)
    rows = g.db.query(Stream).order_by(Stream.id).all()
    return jsonify(
        {
            "capacity": decision.as_dict(),
            "streams": [
                {
                    "id": row.id,
                    "name": row.name,
                    "channel_id": row.channel_id,
                    "status": row.status,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
        }
    )


@bp.get("/api/sanket/status")
@login_required
def sanket_status():
    settings = current_app.config["SETTINGS"]
    live = load_sanket_status(settings.data_dir)
    configured = [redact_broker(url) for url in resolve_brokers(settings, g.db)]
    rows = []
    for channel in g.db.query(Channel).filter_by(audio_backend="sanketptt").order_by(Channel.id).all():
        device_id, _backend = channel_endpoint(channel)
        name = device_id.split(":", 1)[-1]
        heard = (live.get("channels") or {}).get(name) or {}
        rows.append(
            {
                "id": channel.id,
                "name": channel.name,
                "channel": name,
                "enabled": bool(channel.enabled),
                "online": heard.get("online", 0),
                "talking": heard.get("talking") or "",
                "last_talker": heard.get("last_talker") or "",
                "last_packet": heard.get("last_packet") or 0,
            }
        )
    return jsonify(
        {
            "connected": bool(live.get("connected")),
            "power": live.get("power") or "",
            "broker": redact_broker(live.get("broker") or live.get("broker_url") or ""),
            "brokers": [redact_broker(url) for url in (live.get("brokers") or configured)],
            "configured_brokers": configured,
            "last_error": live.get("last_error") or "",
            "reconnects": live.get("reconnects") or 0,
            "backoff_s": live.get("backoff_s") or 0,
            "subscribed": live.get("subscribed") or [],
            "streams": rows,
        }
    )


@bp.post("/api/sanket/test")
@login_required
def sanket_test():
    settings = current_app.config["SETTINGS"]
    result = probe_connection(resolve_brokers(settings, g.db), timeout=5.0)
    return jsonify(result), (200 if result["ok"] else 502)
