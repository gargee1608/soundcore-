import io

from flask import Blueprint, current_app, g, jsonify, request, send_file

from soundcore.auth import login_required
from soundcore.transfer import build_export_zip, import_zip

bp = Blueprint("transfer_api", __name__)


@bp.get("/api/export")
@login_required
def export_data():
    settings = current_app.config["SETTINGS"]
    payload = build_export_zip(g.db, settings.data_dir)
    return send_file(
        io.BytesIO(payload),
        mimetype="application/zip",
        as_attachment=True,
        download_name="soundcore-export.zip",
    )


@bp.post("/api/import")
@login_required
def import_data():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "Choose an export zip"}), 400
    settings = current_app.config["SETTINGS"]
    try:
        summary = import_zip(g.db, upload.read(), settings.data_dir)
    except Exception as exc:
        g.db.rollback()
        return jsonify({"error": str(exc)}), 400
    return jsonify(summary)
