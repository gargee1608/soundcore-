from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, send_file

from soundcore.auth import login_required
from soundcore.models import Channel, Recording, S3Object, Transcription, utcnow
from soundcore.transcription.worker import enqueue_transcription

bp = Blueprint("recordings_api", __name__)


def _touch(recording: Recording) -> None:
    recording.last_accessed_at = utcnow()


@bp.get("/api/recordings")
@login_required
def list_recordings():
    channel_id = request_channel()
    query = g.db.query(Recording).order_by(Recording.started_at.desc())
    if channel_id:
        query = query.filter(Recording.channel_id == channel_id)
    rows = query.limit(200).all()
    channels = {row.id: row.name for row in g.db.query(Channel).all()}
    transcripts = {
        row.recording_id: row
        for row in g.db.query(Transcription).filter(Transcription.recording_id.in_([r.id for r in rows] or [0])).all()
    }
    return jsonify({"recordings": [_public(row, channels, transcripts.get(row.id)) for row in rows]})


@bp.get("/api/recordings/<int:recording_id>/audio")
@login_required
def audio(recording_id: int):
    recording = g.db.get(Recording, recording_id)
    if recording is None or not recording.local_path:
        return jsonify({"error": "Recording not found"}), 404
    _touch(recording)
    settings = current_app.config["SETTINGS"]
    path = Path(settings.data_dir) / recording.local_path
    if path.is_file():
        return send_file(path, mimetype="audio/wav", as_attachment=False, download_name=recording.filename)
    s3_row = g.db.query(S3Object).filter_by(recording_id=recording.id).one_or_none()
    if s3_row and s3_row.verified_at and settings.s3_configured:
        from soundcore.storage.s3 import S3Uploader

        url = S3Uploader.from_settings(settings).presign_get(s3_row.key, settings.s3_presign_seconds)
        return jsonify({"remote": True, "url": url, "filename": recording.filename})
    return jsonify({"error": "Local file is gone and no verified remote copy is available"}), 404


@bp.post("/api/recordings/<int:recording_id>/transcribe")
@login_required
def queue_transcription(recording_id: int):
    recording = g.db.get(Recording, recording_id)
    if recording is None:
        return jsonify({"error": "Recording not found"}), 404
    if recording.storage_state == "writing":
        return jsonify({"error": "This file is still being written"}), 409
    job = enqueue_transcription(g.db, recording)
    return jsonify({"job_id": job.id, "status": job.status})


def request_channel():
    from flask import request

    raw = request.args.get("channel_id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _public(recording: Recording, channels: dict, transcript: Transcription | None) -> dict:
    return {
        "id": recording.id,
        "channel_id": recording.channel_id,
        "channel": channels.get(recording.channel_id, ""),
        "stream_id": recording.stream_id,
        "filename": recording.filename,
        "started_at": recording.started_at.isoformat() if recording.started_at else None,
        "ended_at": recording.ended_at.isoformat() if recording.ended_at else None,
        "duration_s": recording.duration_s,
        "size_bytes": recording.size_bytes,
        "storage_state": recording.storage_state,
        "checksum": recording.checksum,
        "transcript_status": transcript.status if transcript else None,
        "transcript": transcript.text if transcript else None,
        "transcript_error": transcript.error if transcript else None,
    }
