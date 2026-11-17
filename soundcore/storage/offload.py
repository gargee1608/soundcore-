"""Move one recording to S3 and delete the local file only after the database says verified."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from soundcore.models import Event, Recording, S3Object, StorageState, utcnow
from soundcore.storage.guard import RecordingView, delete_local_if_verified
from soundcore.storage.s3 import S3Uploader

logger = logging.getLogger(__name__)


def view_from_row(rec: Recording, verified_at: datetime | None, *, playing: bool = False) -> RecordingView:
    return RecordingView(
        id=rec.id,
        storage_state=rec.storage_state,
        started_at=rec.started_at,
        ended_at=rec.ended_at,
        last_accessed_at=rec.last_accessed_at,
        verified_at=verified_at,
        local_path=rec.local_path,
        playing=playing,
    )


def _add_event(session: Session, level: str, kind: str, message: str, payload: dict | None = None) -> Event:
    event = Event(
        level=level,
        kind=kind,
        message=message,
        payload_json=json.dumps(payload or {}, default=str),
        created_at=utcnow(),
    )
    session.add(event)
    return event


def offload_recording(
    session: Session,
    recording: Recording,
    uploader: S3Uploader,
    data_dir: Path,
    *,
    delete_local: bool = True,
    playing: bool = False,
) -> str:
    """Upload, verify, mark, then maybe delete.

    Returns one of: deleted, verified_kept, kept, skipped.
    `kept` means the local file is still the only trustworthy copy.
    """
    if recording.storage_state == StorageState.WRITING or recording.ended_at is None:
        return "skipped"
    if recording.storage_state == StorageState.LOCAL_DELETED:
        return "skipped"
    if not recording.local_path:
        return "skipped"

    s3_row = session.query(S3Object).filter_by(recording_id=recording.id).one_or_none()
    local_file = Path(data_dir) / recording.local_path

    if recording.storage_state != StorageState.VERIFIED_REMOTE or s3_row is None or s3_row.verified_at is None:
        if not local_file.is_file():
            _add_event(
                session,
                "error",
                "s3_offload",
                f"Cannot offload {recording.filename}: local file is missing and remote copy is not verified.",
                {"recording_id": recording.id},
            )
            return "kept"
        key = uploader.key_for(recording.local_path)
        result = uploader.upload_and_verify(local_file, key=key)
        if not result.ok:
            if result.uploaded:
                recording.storage_state = StorageState.UPLOADED_UNVERIFIED
            _add_event(
                session,
                "error",
                "s3_verify_failed",
                f"S3 verification failed for {recording.filename}. Local file kept.",
                {
                    "recording_id": recording.id,
                    "stage": result.stage,
                    "error": result.error,
                    "etag": result.etag,
                    "remote_size": result.remote_size,
                    "local_size": result.local_size,
                },
            )
            session.flush()
            logger.warning("verify failed for recording %s: %s", recording.id, result.error)
            return "kept"
        now = utcnow()
        if s3_row is None:
            s3_row = S3Object(recording_id=recording.id, bucket=uploader.bucket, key=key)
            session.add(s3_row)
            session.flush()
        s3_row.bucket = uploader.bucket
        s3_row.key = key
        s3_row.etag = result.etag
        s3_row.size_bytes = result.remote_size
        s3_row.uploaded_at = now
        s3_row.verified_at = now
        s3_row.verify_method = result.verify_method
        recording.s3_object_id = s3_row.id
        recording.storage_state = StorageState.VERIFIED_REMOTE
        # Commit the verified mark before unlinking so a crash cannot lose the
        # only copy while the database still says it was never verified.
        session.commit()
        from soundcore.activity import record_activity

        record_activity("upload_done", f"Verified upload {key}", channel=str(recording.channel_id))

    if not delete_local:
        return "verified_kept"

    session.refresh(recording)
    verified_at = s3_row.verified_at if s3_row is not None else None
    snapshot = view_from_row(recording, verified_at, playing=playing)
    removed, reason = delete_local_if_verified(snapshot, data_dir)
    if not removed:
        logger.info("local delete skipped for %s: %s", recording.id, reason)
        return "verified_kept" if snapshot.storage_state == StorageState.VERIFIED_REMOTE else "kept"
    recording.storage_state = StorageState.LOCAL_DELETED
    session.commit()
    return "deleted"
