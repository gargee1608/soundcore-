from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from soundcore.db import create_all, init_engine, session_scope
from soundcore.models import Channel, Recording, S3Object, StorageState, utcnow


@pytest.fixture
def data_dir(tmp_path):
    root = tmp_path / "data"
    (root / "recordings").mkdir(parents=True)
    init_engine(f"sqlite:///{tmp_path / 'soundcore.db'}", force=True)
    create_all()
    return root


@pytest.fixture
def now():
    return datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def add_channel(session, name="Radio 1", slug="radio-1", direction="in", enabled=True):
    channel = Channel(
        name=name,
        slug=slug,
        direction=direction,
        alsa_device=f"sim:{slug}",
        usb_id="test",
        device_kind="usb",
        sample_rate=16000,
        format="s16le",
        gain_db=0,
        enabled=enabled,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(channel)
    session.flush()
    return channel


def add_recording(
    session,
    data_dir: Path,
    channel,
    *,
    state=StorageState.LOCAL_ONLY,
    started_at=None,
    ended_at=None,
    accessed_at=None,
    verified_at=None,
    name="clip.wav",
    body=b"RIFFdemo",
):
    started_at = started_at or utcnow()
    relative = f"recordings/{channel.slug}/{name}"
    path = data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    recording = Recording(
        channel_id=channel.id,
        filename=name,
        local_path=relative,
        started_at=started_at,
        ended_at=ended_at if state != StorageState.WRITING else None,
        duration_s=1.0 if state != StorageState.WRITING else None,
        size_bytes=len(body),
        checksum="abc",
        last_accessed_at=accessed_at,
        storage_state=state,
    )
    session.add(recording)
    session.flush()
    if verified_at is not None:
        s3 = S3Object(
            recording_id=recording.id,
            bucket="soundcore-test",
            key=f"soundcore/{relative}",
            etag="abc",
            size_bytes=len(body),
            uploaded_at=verified_at,
            verified_at=verified_at,
            verify_method="etag_md5_and_size",
        )
        session.add(s3)
        session.flush()
        recording.s3_object_id = s3.id
    return recording


def days_ago(now, days):
    return now - timedelta(days=days)
