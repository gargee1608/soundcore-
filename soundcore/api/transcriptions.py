from flask import Blueprint, g, jsonify

from soundcore.auth import login_required
from soundcore.models import Transcription

bp = Blueprint("transcriptions_api", __name__)


@bp.get("/api/transcriptions")
@login_required
def list_transcriptions():
    rows = g.db.query(Transcription).order_by(Transcription.id.desc()).limit(200).all()
    return jsonify(
        {
            "transcriptions": [
                {
                    "id": row.id,
                    "recording_id": row.recording_id,
                    "engine": row.engine,
                    "status": row.status,
                    "text": row.text,
                    "error": row.error,
                    "attempt_count": row.attempt_count,
                    "requested_at": row.requested_at.isoformat() if row.requested_at else None,
                    "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                    "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
                }
                for row in rows
            ]
        }
    )
