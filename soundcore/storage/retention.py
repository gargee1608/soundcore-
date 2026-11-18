"""Delete closed local recordings older than a configured number of days.

Zero days means retention is off. Open files are never removed. A file that
already has a verified remote copy is marked remote-only. A file with no
remote copy is removed with its row.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from soundcore.models import Recording, S3Object, StorageState, utcnow


def cleanup_local_retention(session, data_dir, *, retention_days: int, now=None) -> int:
    if retention_days <= 0:
        return 0
    now = now or utcnow()
    cutoff = now - timedelta(days=retention_days)
    rows = (
        session.query(Recording)
        .filter(
            Recording.ended_at.is_not(None),
            Recording.ended_at < cutoff,
            Recording.storage_state != StorageState.WRITING,
            Recording.storage_state != StorageState.LOCAL_DELETED,
        )
        .all()
    )
    removed = 0
    root = Path(data_dir)
    for row in rows:
        path = root / (row.local_path or "")
        if row.local_path and path.is_file():
            path.unlink()
        remote = session.query(S3Object).filter_by(recording_id=row.id).one_or_none()
        if remote is not None and remote.verified_at is not None:
            row.storage_state = StorageState.LOCAL_DELETED
            row.local_path = None
        else:
            session.delete(row)
        removed += 1
    if removed:
        session.flush()
    return removed
