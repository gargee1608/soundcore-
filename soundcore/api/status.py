from flask import Blueprint, current_app, g, jsonify

from soundcore.auth import login_required
from soundcore.status import console_status

bp = Blueprint("status_api", __name__)


@bp.get("/api/status")
@login_required
def status():
    payload = console_status(g.db, current_app.config["SETTINGS"], current_app.extensions.get("audio"))
    return jsonify(payload)
