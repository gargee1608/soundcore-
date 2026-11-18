"""Local retention removes closed files and leaves open ones."""

from datetime import timedelta

from soundcore.db import session_scope
from soundcore.models import Recording, StorageState, utcnow
from soundcore.storage.retention import cleanup_local_retention
from tests.conftest import add_channel, add_recording


def test_retention_deletes_old_closed_files_only(data_dir):
    with session_scope() as db:
        channel = add_channel(db)
        old = add_recording(
            db,
            data_dir,
            channel,
            state=StorageState.LOCAL_ONLY,
            ended_at=utcnow() - timedelta(days=10),
            name="old.wav",
        )
        fresh = add_recording(
            db,
            data_dir,
            channel,
            state=StorageState.LOCAL_ONLY,
            ended_at=utcnow() - timedelta(days=1),
            name="fresh.wav",
        )
        open_row = add_recording(
            db,
            data_dir,
            channel,
            state=StorageState.WRITING,
            name="open.wav",
        )
        old_id, fresh_id, open_id = old.id, fresh.id, open_row.id
        removed = cleanup_local_retention(db, data_dir, retention_days=7)
        assert removed == 1
        assert db.get(Recording, old_id) is None
        assert db.get(Recording, fresh_id) is not None
        assert db.get(Recording, open_id) is not None
        assert not (data_dir / "recordings" / channel.slug / "old.wav").is_file()
        assert (data_dir / "recordings" / channel.slug / "fresh.wav").is_file()
