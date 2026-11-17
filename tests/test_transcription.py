from datetime import timedelta
from pathlib import Path

import requests

from soundcore.db import session_scope
from soundcore.models import Job, JobStatus, Transcription, TranscriptionStatus
from soundcore.transcription.base import TranscriptionError, classify_http, next_backoff_seconds
from soundcore.transcription.client import BoondockClient
from soundcore.transcription.worker import enqueue_transcription, process_transcription_job, run_due_transcriptions
from tests.conftest import add_channel, add_recording


class ScriptedEngine:
    name = "boondock"

    def __init__(self, results):
        self.results = list(results)
        self.file_calls = 0
        self.s3_calls = 0

    def transcribe_file(self, path, *, language=""):
        self.file_calls += 1
        return self._next()

    def transcribe_s3_url(self, s3_url, *, language=""):
        self.s3_calls += 1
        return self._next()

    def _next(self):
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeResponse:
    def __init__(self, status, body, text=""):
        self.status_code = status
        self._body = body
        self.text = text or str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _ready(session, data_dir, body=b"audio"):
    channel = add_channel(session)
    recording = add_recording(
        session,
        data_dir,
        channel,
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ended_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        name="clip.wav",
        body=body,
    )
    job = enqueue_transcription(session, recording)
    return recording, job


def test_classify_and_backoff():
    assert classify_http(200) == "ok"
    assert classify_http(500) == "retry"
    assert classify_http(403) == "fatal"
    assert classify_http(413) == "too_large"
    assert classify_http(None, timed_out=True) == "retry"
    assert next_backoff_seconds(1) == 30
    assert next_backoff_seconds(2) == 60
    assert next_backoff_seconds(10) == 900


def test_success_and_retry_then_success(data_dir, now):
    engine = ScriptedEngine([
        TranscriptionError("provider down", retryable=True, status_code=500, kind="retry"),
        "engine one on the bridge",
    ])
    with session_scope() as session:
        _recording, job = _ready(session, data_dir)
        first = process_transcription_job(session, job, engine=engine, data_dir=data_dir, now=now, max_attempts=5)
        row = session.query(Transcription).one()
        assert first == "retry"
        assert row.status == TranscriptionStatus.RETRYING
        assert row.attempt_count == 1
        assert job.status == JobStatus.PENDING
        second = process_transcription_job(
            session, job, engine=engine, data_dir=data_dir, now=now + timedelta(minutes=5), max_attempts=5
        )
        assert second == "done"
        assert row.text == "engine one on the bridge"
        assert row.status == TranscriptionStatus.DONE


def test_retries_stop_at_the_cap(data_dir, now):
    engine = ScriptedEngine([
        TranscriptionError("still down", retryable=True, status_code=500, kind="retry"),
        TranscriptionError("still down", retryable=True, status_code=500, kind="retry"),
    ])
    with session_scope() as session:
        _recording, job = _ready(session, data_dir)
        process_transcription_job(session, job, engine=engine, data_dir=data_dir, now=now, max_attempts=2)
        outcome = process_transcription_job(
            session, job, engine=engine, data_dir=data_dir, now=now + timedelta(minutes=2), max_attempts=2
        )
        row = session.query(Transcription).one()
        assert outcome == "failed"
        assert row.status == TranscriptionStatus.FAILED
        assert row.attempt_count == 2
        assert job.status == JobStatus.FAILED
        # A later pass must not pick the failed job up again.
        again = run_due_transcriptions(
            session, engine=engine, data_dir=data_dir, now=now + timedelta(days=1), max_attempts=2
        )
        assert again == []


def test_403_and_413_do_not_retry(data_dir, now):
    forbidden = ScriptedEngine([
        TranscriptionError("Invalid secret key", retryable=False, status_code=403, kind="fatal"),
    ])
    with session_scope() as session:
        _recording, job = _ready(session, data_dir, body=b"a")
        outcome = process_transcription_job(session, job, engine=forbidden, data_dir=data_dir, now=now)
        row = session.query(Transcription).filter_by(recording_id=_recording.id).one()
        assert outcome == "failed"
        assert row.attempt_count == 1
        assert row.status == TranscriptionStatus.FAILED
        assert run_due_transcriptions(session, engine=forbidden, data_dir=data_dir, now=now + timedelta(hours=1)) == []

    too_large = ScriptedEngine([
        TranscriptionError("Request too large. Maximum size: 2MB", retryable=False, status_code=413, kind="too_large"),
    ])
    with session_scope() as session:
        channel_recording = add_recording(
            session,
            data_dir,
            add_channel(session, name="FM", slug="fm"),
            started_at=now,
            ended_at=now,
            name="big.wav",
            body=b"small-but-rejected",
        )
        job = enqueue_transcription(session, channel_recording)
        outcome = process_transcription_job(session, job, engine=too_large, data_dir=data_dir, now=now)
        row = session.query(Transcription).filter_by(recording_id=channel_recording.id).one()
        assert outcome == "failed"
        assert too_large.s3_calls == 0
        assert row.status == TranscriptionStatus.FAILED
        assert job.status == JobStatus.FAILED


def test_413_uses_s3_url_once(data_dir, now):
    engine = ScriptedEngine([
        TranscriptionError("too big", retryable=False, status_code=413, kind="too_large"),
        "transcribed from the bucket",
    ])
    with session_scope() as session:
        recording, job = _ready(session, data_dir, body=b"wav")
        outcome = process_transcription_job(
            session,
            job,
            engine=engine,
            data_dir=data_dir,
            now=now,
            presign=lambda bucket, key: "https://files.example/audio.wav",
        )
        # No verified S3 row, so presign is not called and 413 fails closed.
        assert outcome == "failed"
        assert engine.s3_calls == 0

    engine = ScriptedEngine(["via s3"])
    with session_scope() as session:
        channel = add_channel(session, name="Handheld", slug="handheld")
        recording = add_recording(
            session, data_dir, channel, started_at=now, ended_at=now, name="huge.wav", body=b"x" * 32
        )
        recording.size_bytes = 3 * 1024 * 1024
        from soundcore.models import S3Object

        s3 = S3Object(
            recording_id=recording.id,
            bucket="soundcore-test",
            key="soundcore/huge.wav",
            etag="abc",
            size_bytes=recording.size_bytes,
            uploaded_at=now,
            verified_at=now,
            verify_method="etag_md5_and_size",
        )
        session.add(s3)
        session.flush()
        job = enqueue_transcription(session, recording)
        outcome = process_transcription_job(
            session,
            job,
            engine=engine,
            data_dir=data_dir,
            now=now,
            max_bytes=2 * 1024 * 1024,
            presign=lambda bucket, key: f"https://files.example/{key}",
        )
        row = session.query(Transcription).filter_by(recording_id=recording.id).one()
        assert outcome == "done"
        assert engine.file_calls == 0
        assert engine.s3_calls == 1
        assert row.text == "via s3"


def test_timeout_is_retryable(data_dir, now):
    engine = ScriptedEngine([
        TranscriptionError("Boondock request timed out", retryable=True, kind="retry"),
    ])
    with session_scope() as session:
        _recording, job = _ready(session, data_dir, body=b"t")
        outcome = process_transcription_job(session, job, engine=engine, data_dir=data_dir, now=now, max_attempts=4)
        assert outcome == "retry"
        assert job.next_run_at > now


def test_boondock_client_contract(tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFFxxxx")
    http = FakeHttp(FakeResponse(200, {"transcription": "copy that"}))
    client = BoondockClient("https://api.boondock.cloud", "customer-key", http=http)
    assert client.transcribe_file(str(audio), language="en") == "copy that"
    url, kwargs = http.calls[0]
    assert url == "https://api.boondock.cloud/api/v3/transcribe/openai"
    assert kwargs["headers"]["X-Boondock-Key"] == "customer-key"
    assert "audio_file" in kwargs["files"]
    assert kwargs["data"]["language"] == "en"

    http = FakeHttp(FakeResponse(403, {"error": "Invalid secret key"}))
    client = BoondockClient("https://api.boondock.cloud/", "bad", http=http)
    try:
        client.transcribe_file(str(audio))
        assert False
    except TranscriptionError as exc:
        assert exc.status_code == 403
        assert exc.retryable is False

    http = FakeHttp(requests.Timeout("slow"))
    client = BoondockClient("https://api.boondock.cloud", "k", http=http)
    try:
        client.transcribe_s3_url("https://bucket/file.wav")
        assert False
    except TranscriptionError as exc:
        assert exc.retryable is True
    assert http.calls[0][0].endswith("/api/v3/s3/transcribe/openai")
    assert http.calls[0][1]["json"]["s3_url"] == "https://bucket/file.wav"


def test_missing_api_key_waits_without_burning_attempts(data_dir, now):
    engine = ScriptedEngine(["should not run"])
    with session_scope() as session:
        _recording, job = _ready(session, data_dir)
        outcome = process_transcription_job(
            session, job, engine=engine, data_dir=data_dir, now=now, api_configured=False
        )
        row = session.query(Transcription).one()
        assert outcome == "waiting"
        assert row.status == TranscriptionStatus.PENDING
        assert row.attempt_count == 0
        assert engine.file_calls == 0
        assert "not configured" in (row.error or "")
        assert session.query(Job).count() == 1
