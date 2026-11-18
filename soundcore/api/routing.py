from flask import Blueprint, g, jsonify, request

from soundcore.auth import login_required
from soundcore.models import Route
from soundcore.ops import OpsError, save_route

bp = Blueprint("routing_api", __name__)


@bp.get("/api/routes")
@login_required
def list_routes():
    rows = g.db.query(Route).order_by(Route.id).all()
    return jsonify(
        {
            "routes": [
                {
                    "id": row.id,
                    "input_channel_id": row.input_channel_id,
                    "output_channel_id": row.output_channel_id,
                    "enabled": row.enabled,
                    "gain_db": row.gain_db,
                }
                for row in rows
            ]
        }
    )


@bp.post("/api/routes")
@login_required
def upsert_route():
    payload = request.get_json(silent=True) or {}
    try:
        route = save_route(
            g.db,
            int(payload["input_channel_id"]),
            int(payload["output_channel_id"]),
            enabled=bool(payload.get("enabled", True)),
            gain_db=float(payload.get("gain_db") or 0),
        )
    except (OpsError, KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"id": route.id, "enabled": route.enabled, "gain_db": route.gain_db})


@bp.post("/api/routes/<int:route_id>/delete")
@login_required
def delete_route(route_id: int):
    route = g.db.get(Route, route_id)
    if route is None:
        return jsonify({"error": "Route not found"}), 404
    g.db.delete(route)
    return jsonify({"result": "deleted"})
