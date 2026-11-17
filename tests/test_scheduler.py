from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from soundcore.db import session_scope
from soundcore.models import StorageState
from soundcore.storage.guard import RecordingView, midnight_candidates
from soundcore.storage.s3 import VerifyResult
from soundcore.storage.scheduler import (
    GUARD_JOB_ID,
    MIDNIGHT_JOB_ID,
    build_scheduler,
    register_jobs,
    run_midnight_offload,
)
from tests.conftest import add_channel, add_recording


class OkUploader:
    bucket = "soundcore-test"
    prefix = "soundcore"

    def key_for(self, relative_path: str) -> str:
        return f"soundcore/{relative_path}"

    def upload_and_verify(self, local_path, key=None):
        size = local_path.stat().st_size if hasattr(local_path, "stat") else 1
        return VerifyResult(ok=True, uploaded=True, etag="abc123", remote_size=size, local_size=size, local_md5="abc123")


def _view(recording_id, state, started, ended, accessed):
    return RecordingView(
        id=recording_id,
        storage_state=state,
        started_at=started,
        ended_at=ended,
        last_accessed_at=accessed,
        verified_at=None,
        local_path="recordings/radio-1/x.wav",
    )


def test_midnight_job_is_wired_to_local_midnight():
    scheduler = build_scheduler("America/Chicago")
    register_jobs(scheduler, "America/Chicago", lambda: None, lambda: None, guard_interval_seconds=60)
    midnight = scheduler.get_job(MIDNIGHT_JOB_ID)
    guard = scheduler.get_job(GUARD_JOB_ID)
    assert midnight is not None and guard is not None
    zone = ZoneInfo("America/Chicago")
    now = datetime(2026, 11, 17, 15, 30, tzinfo=zone)
    nxt = midnight.trigger.get_next_fire_time(None, now)
    assert nxt.hour == 0 and nxt.minute == 0 and nxt.second == 0
    assert nxt.date() == datetime(2026, 11, 18).date()
    assert nxt.tzinfo.key == "America/Chicago"


def test_midnight_selects_only_idle_closed_files():
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    idle = _view(1, StorageState.LOCAL_ONLY, now - timedelta(days=12), now - timedelta(days=12), now - timedelta(days=9))
    recent = _view(2, StorageState.LOCAL_ONLY, now - timedelta(days=3), now - timedelta(days=3), now - timedelta(days=1))
    open_rec = _view(3, StorageState.WRITING, now - timedelta(days=20), None, None)
    chosen = midnight_candidates([idle, recent, open_rec], now, idle_days=7)
    assert [item.id for item in chosen] == [1]


def test_midnight_offload_skips_open_recordings(data_dir):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    with session_scope() as session:
        channel = add_channel(session)
        idle = add_recording(
            session,
            data_dir,
            channel,
            state=StorageState.LOCAL_ONLY,
            started_at=now - timedelta(days=10),
            ended_at=now - timedelta(days=10),
            accessed_at=now - timedelta(days=8),
            name="idle.wav",
            body=b"idle-audio",
        )
        current = add_recording(
            session,
            data_dir,
            channel,
            state=StorageState.WRITING,
            started_at=now,
            name="writing.wav",
            body=b"still-open",
        )
        idle_path = data_dir / idle.local_path
        open_path = data_dir / current.local_path
        summary = run_midnight_offload(session, OkUploader(), data_dir, now=now, idle_days=7)
        session.refresh(idle)
        session.refresh(current)
        assert summary["deleted"] == 1
        assert not idle_path.exists()
        assert idle.storage_state == StorageState.LOCAL_DELETED
        assert open_path.read_bytes() == b"still-open"
        assert current.storage_state == StorageState.WRITING
