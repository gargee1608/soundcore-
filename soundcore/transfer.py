"""Export and import the database plus local audio references.

IDs are preserved so recording, channel, transcript, and S3 associations survive a restore.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from soundcore.models import (
    Channel,
    Event,
    Job,
    Recording,
    Route,
    S3Object,
    Setting,
    Stream,
    Transcription,
    User,
)

EXPORT_VERSION = 1

_TABLES = [
    ("users", User),
    ("channels", Channel),
    ("settings", Setting),
    ("streams", Stream),
    ("recordings", Recording),
    ("s3_objects", S3Object),
    ("transcriptions", Transcription),
    ("routes", Route),
    ("job_queue", Job),
    ("events", Event),
]

# Child tables first so a wipe respects foreign keys.
_DELETE_ORDER = [
    Job,
    Transcription,
    S3Object,
    Recording,
    Route,
    Stream,
    Event,
    Setting,
    Channel,
    User,
]


def _iso(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def _parse_dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "started_at",
    "ended_at",
    "last_accessed_at",
    "requested_at",
    "completed_at",
    "next_retry_at",
    "uploaded_at",
    "verified_at",
    "acked_at",
    "last_login_at",
    "next_run_at",
}


def _row_dict(obj) -> dict:
    data = {}
    for column in obj.__table__.columns:
        data[column.name] = _iso(getattr(obj, column.name))
    return data


def export_manifest(session) -> dict:
    tables = {}
    for name, model in _TABLES:
        sort_col = model.key if name == "settings" else model.id
        tables[name] = [_row_dict(row) for row in session.query(model).order_by(sort_col).all()]
    audio = []
    for recording in session.query(Recording).order_by(Recording.id).all():
        s3 = session.query(S3Object).filter_by(recording_id=recording.id).one_or_none()
        audio.append(
            {
                "recording_id": recording.id,
                "channel_id": recording.channel_id,
                "stream_id": recording.stream_id,
                "filename": recording.filename,
                "local_path": recording.local_path,
                "checksum": recording.checksum,
                "size_bytes": recording.size_bytes,
                "storage_state": recording.storage_state,
                "s3_bucket": s3.bucket if s3 else None,
                "s3_key": s3.key if s3 else None,
                "s3_object_id": recording.s3_object_id,
            }
        )
    return {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
        "audio_manifest": audio,
    }


def build_export_zip(session, data_dir: Path) -> bytes:
    manifest = export_manifest(session)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for item in manifest["audio_manifest"]:
            relative = item.get("local_path")
            if not relative:
                continue
            path = Path(data_dir) / relative
            if path.is_file():
                archive.write(path, arcname=relative.replace("\\", "/"))
    return buffer.getvalue()


def _wipe(session) -> None:
    for model in _DELETE_ORDER:
        session.query(model).delete()
    session.flush()
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        found = session.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")).first()
        if found:
            session.execute(text("DELETE FROM sqlite_sequence"))
            session.flush()


def _insert_rows(session, model, rows: list[dict]) -> None:
    for raw in rows:
        payload = {}
        for column in model.__table__.columns:
            if column.name not in raw:
                continue
            value = raw[column.name]
            if column.name in _DATETIME_FIELDS:
                value = _parse_dt(value)
            payload[column.name] = value
        session.add(model(**payload))
    session.flush()


def import_manifest(session, manifest: dict, data_dir: Path, archive: zipfile.ZipFile | None = None) -> dict:
    if manifest.get("version") != EXPORT_VERSION:
        raise ValueError("Unsupported export version")
    tables = manifest.get("tables") or {}
    _wipe(session)
    for name, model in _TABLES:
        _insert_rows(session, model, tables.get(name) or [])
    restored_files = 0
    missing = 0
    if archive is not None:
        for item in manifest.get("audio_manifest") or []:
            relative = item.get("local_path")
            if not relative:
                continue
            arcname = relative.replace("\\", "/")
            if arcname not in archive.namelist():
                if item.get("storage_state") != "local_deleted_remote_only":
                    missing += 1
                continue
            target = Path(data_dir) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(arcname))
            restored_files += 1
    return {
        "recordings": len(tables.get("recordings") or []),
        "channels": len(tables.get("channels") or []),
        "transcriptions": len(tables.get("transcriptions") or []),
        "files_restored": restored_files,
        "files_missing": missing,
    }


def import_zip(session, payload: bytes, data_dir: Path) -> dict:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        return import_manifest(session, manifest, data_dir, archive)
