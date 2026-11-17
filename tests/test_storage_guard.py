from datetime import timedelta
from pathlib import Path

from soundcore.db import session_scope
from soundcore.models import StorageState
from soundcore.storage.guard import (
    DiskUsage,
    RecordingView,
    admission,
    delete_local_if_verified,
    emergency_candidates,
    needs_emergency_cleanup,
    safe_to_delete_local,
)
from soundcore.storage.s3 import VerifyResult
from soundcore.storage.scheduler import run_storage_guard
from tests.conftest import add_channel, add_recording, days_ago


class BadUploader:
    bucket = "soundcore-test"
    prefix = "soundcore"

    def key_for(self, relative_path: str) -> str:
        return f"soundcore/{relative_path}"

    def upload_and_verify(self, local_path, key=None):
        return VerifyResult(
            ok=False,
            uploaded=True,
            stage="verify",
            error="etag or size mismatch",
            etag="deadbeef",
            remote_size=1,
            local_size=Path(local_path).stat().st_size,
            local_md5="abc",
        )


def test_reserve_and_threshold():
    assert admission(DiskUsage(used=50, total=100)) == "ok"
    assert admission(DiskUsage(used=92, total=100)) == "warn"  # 8% free, within 2x of 5%
    assert admission(DiskUsage(used=95, total=100)) == "refuse"
    assert admission(DiskUsage(used=99, total=100)) == "refuse"
    assert needs_emergency_cleanup(DiskUsage(used=95, total=100)) is True
    assert needs_emergency_cleanup(DiskUsage(used=94, total=100)) is False


def _view(**kwargs):
    from datetime import datetime, timezone

    started = datetime(2026, 11, 1, tzinfo=timezone.utc)
    defaults = dict(
        id=1,
        storage_state=StorageState.LOCAL_ONLY,
        started_at=started,
        ended_at=started,
        last_accessed_at=started,
        verified_at=None,
        local_path="recordings/radio-1/clip.wav",
        playing=False,
    )
    defaults.update(kwargs)
    return RecordingView(**defaults)


def test_unverified_states_are_not_deletable(data_dir):
    for state in (StorageState.LOCAL_ONLY, StorageState.UPLOADED_UNVERIFIED, StorageState.WRITING):
        view = _view(storage_state=state, ended_at=None if state == StorageState.WRITING else _view().ended_at)
        allowed, reason = safe_to_delete_local(view)
        assert allowed is False
        assert reason != "ok"
    verified = _view(
        storage_state=StorageState.VERIFIED_REMOTE,
        verified_at=_view().started_at,
    )
    assert safe_to_delete_local(verified)[0] is True
    path = data_dir / verified.local_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"keep-me")
    removed, reason = delete_local_if_verified(
        _view(storage_state=StorageState.LOCAL_ONLY),
        data_dir,
    )
    assert removed is False
    assert path.is_file()
    removed, reason = delete_local_if_verified(verified, data_dir)
    assert removed is True
    assert not path.is_file()
    assert reason == "deleted"


def test_cleanup_never_deletes_unverified_files(data_dir, now):
    alerts = []
    with session_scope() as session:
        channel = add_channel(session)
        verified = add_recording(
            session,
            data_dir,
            channel,
            state=StorageState.VERIFIED_REMOTE,
            started_at=days_ago(now, 10),
            ended_at=days_ago(now, 10),
            accessed_at=days_ago(now, 9),
            verified_at=days_ago(now, 9),
            name="verified.wav",
            body=b"verified-bytes",
        )
        unverified = add_recording(
            session,
            data_dir,
            channel,
            state=StorageState.LOCAL_ONLY,
            started_at=days_ago(now, 8),
            ended_at=days_ago(now, 8),
            accessed_at=days_ago(now, 8),
            name="local.wav",
            body=b"local-bytes",
        )
        mismatched = add_recording(
            session,
            data_dir,
            channel,
            state=StorageState.UPLOADED_UNVERIFIED,
            started_at=days_ago(now, 4),
            ended_at=days_ago(now, 4),
            name="bad.wav",
            body=b"bad-bytes",
        )
        open_rec = add_recording(
            session,
            data_dir,
            channel,
            state=StorageState.WRITING,
            started_at=now,
            name="open.wav",
            body=b"open-bytes",
        )
        verified_path = data_dir / verified.local_path
        local_path = data_dir / unverified.local_path
        bad_path = data_dir / mismatched.local_path
        open_path = data_dir / open_rec.local_path
        summary = run_storage_guard(
            session,
            BadUploader(),
            data_dir,
            now=now,
            usage=__import__("soundcore.storage.guard", fromlist=["DiskUsage"]).DiskUsage(used=97, total=100),
            notify=lambda *_args, **_kwargs: alerts.append(_args[1] if False else _args),
        )
    assert summary["triggered"] is True
    assert summary["deleted"] == 1
    assert not verified_path.exists()
    assert local_path.read_bytes() == b"local-bytes"
    assert bad_path.read_bytes() == b"bad-bytes"
    assert open_path.read_bytes() == b"open-bytes"
    assert alerts and alerts[0][1] == "warning"


def test_recently_played_files_are_not_emergency_candidates(now):
    fresh = _view(last_accessed_at=now - timedelta(seconds=30), storage_state=StorageState.VERIFIED_REMOTE, verified_at=now)
    stale = _view(id=2, last_accessed_at=now - timedelta(days=3), storage_state=StorageState.LOCAL_ONLY)
    chosen = emergency_candidates([fresh, stale], now, playback_grace=timedelta(minutes=2))
    assert [item.id for item in chosen] == [2]
