from flask import Blueprint, current_app, g, jsonify

from soundcore.auth import login_required
from soundcore.maintenance import run_guard
from soundcore.models import Event, utcnow

bp = Blueprint("storage_api", __name__)


@bp.get("/api/storage")
@login_required
def storage_state():
    from soundcore.status import console_status

    status = console_status(g.db, current_app.config["SETTINGS"], current_app.extensions.get("audio"))
    events = g.db.query(Event).order_by(Event.id.desc()).limit(30).all()
    return jsonify(
        {
            "disk": status["disk"],
            "capacity": status["capacity"],
            "events": [
                {
                    "id": event.id,
                    "level": event.level,
                    "kind": event.kind,
                    "message": event.message,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                    "acked_at": event.acked_at.isoformat() if event.acked_at else None,
                }
                for event in events
            ],
        }
    )


@bp.post("/api/storage/check")
@login_required
def run_check():
    summary = run_guard(current_app.config["SETTINGS"])
    return jsonify(summary)


@bp.post("/api/storage/events/<int:event_id>/ack")
@login_required
def ack(event_id: int):
    event = g.db.get(Event, event_id)
    if event is None:
        return jsonify({"error": "Event not found"}), 404
    event.acked_at = utcnow()
    return jsonify({"id": event.id, "acked_at": event.acked_at.isoformat()})
