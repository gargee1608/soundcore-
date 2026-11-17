from flask import Blueprint, current_app, g, jsonify

from soundcore.auth import login_required
from soundcore.capacity import evaluate_capacity
from soundcore.models import Stream
from soundcore.ops import active_inputs
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
