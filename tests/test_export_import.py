from datetime import timezone

from soundcore.auth import ensure_admin
from soundcore.db import session_scope
from soundcore.models import Channel, Event, Recording, Route, S3Object, Stream, Transcription, TranscriptionStatus, utcnow
from soundcore.transfer import EXPORT_VERSION, build_export_zip, export_manifest, import_zip
from tests.conftest import add_channel, add_recording


def test_round_trip_preserves_recording_associations(data_dir, now):
    with session_scope() as session:
        ensure_admin(session, "admin@example.com", "correct-horse")
        channel = add_channel(session, name="Radio 1", slug="radio-1")
        output = add_channel(session, name="USB speaker", slug="usb-speaker", direction="out")
        stream = Stream(name="Radio 1", channel_id=channel.id, status="active", created_at=utcnow())
        session.add(stream)
        session.flush()
        recording = add_recording(
            session,
            data_dir,
            channel,
            started_at=now,
            ended_at=now,
            name="radio-1_2026-11-17T12-00-00_ab12cd34.wav",
            body=b"RIFF-round-trip",
        )
        recording.stream_id = stream.id
        recording.checksum = "deadbeef"
        s3 = S3Object(
            recording_id=recording.id,
            bucket="soundcore-test",
            key="soundcore/recordings/radio-1/clip.wav",
            etag="etag1",
            size_bytes=len(b"RIFF-round-trip"),
            uploaded_at=now,
            verified_at=now,
            verify_method="etag_md5_and_size",
        )
        session.add(s3)
        session.flush()
        recording.s3_object_id = s3.id
        session.add(
            Transcription(
                recording_id=recording.id,
                engine="boondock",
                status=TranscriptionStatus.DONE,
                text="unit one responding",
                attempt_count=1,
                requested_at=now,
                completed_at=now,
            )
        )
        session.add(Route(input_channel_id=channel.id, output_channel_id=output.id, enabled=True, created_at=utcnow()))
        session.add(Event(level="warning", kind="disk_threshold", message="95%", created_at=utcnow()))
        session.flush()
        original_ids = {
            "channel": channel.id,
            "recording": recording.id,
            "stream": stream.id,
            "s3": s3.id,
            "output": output.id,
        }
        payload = build_export_zip(session, data_dir)
        manifest = export_manifest(session)
        assert manifest["version"] == EXPORT_VERSION
        assert manifest["audio_manifest"][0]["recording_id"] == recording.id
        assert manifest["audio_manifest"][0]["channel_id"] == channel.id

        # Wipe by importing into the same database.
        summary = import_zip(session, payload, data_dir)

    with session_scope() as session:
        assert summary["recordings"] == 1
        assert summary["files_restored"] == 1
        channel = session.query(Channel).filter_by(slug="radio-1").one()
        output = session.query(Channel).filter_by(slug="usb-speaker").one()
        recording = session.query(Recording).one()
        transcript = session.query(Transcription).one()
        s3 = session.query(S3Object).one()
        route = session.query(Route).one()
        stream = session.query(Stream).one()
        assert channel.id == original_ids["channel"]
        assert output.id == original_ids["output"]
        assert recording.id == original_ids["recording"]
        assert recording.channel_id == channel.id
        assert recording.stream_id == stream.id == original_ids["stream"]
        assert recording.s3_object_id == s3.id == original_ids["s3"]
        assert s3.recording_id == recording.id
        assert s3.key == "soundcore/recordings/radio-1/clip.wav"
        assert transcript.recording_id == recording.id
        assert transcript.text == "unit one responding"
        assert route.input_channel_id == channel.id
        assert route.output_channel_id == output.id
        restored = data_dir / recording.local_path
        assert restored.read_bytes() == b"RIFF-round-trip"
        assert recording.checksum == "deadbeef"
        assert session.query(Event).count() == 1
        assert recording.started_at.tzinfo is not None
        assert recording.started_at.astimezone(timezone.utc) == now
