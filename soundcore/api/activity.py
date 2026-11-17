from flask import Blueprint, current_app, g, jsonify, request

from soundcore.activity import build_activity
from soundcore.auth import login_required

bp = Blueprint("activity_api", __name__)


@bp.get("/api/activity")
@login_required
def activity():
    settings = current_app.config["SETTINGS"]
    payload = build_activity(
        g.db,
        settings,
        current_app.extensions.get("audio_backend"),
        kind=(request.args.get("kind") or "").strip(),
        channel=(request.args.get("channel") or "").strip(),
        limit=request.args.get("limit", 200),
    )
    return jsonify(payload)
