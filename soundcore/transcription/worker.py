"""Retry queue for transcription jobs.

500s and timeouts are retried with backoff up to the configured attempt cap.
403 fails immediately. 413 tries the S3 URL endpoint once, then fails.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from soundcore.models import (
    Job,
    JobStatus,
    Recording,
    S3Object,
    StorageState,
    Transcription,
    TranscriptionStatus,
    utcnow,
)
from soundcore.transcription.base import TranscriptionError, next_backoff_seconds

logger = logging.getLogger(__name__)


def enqueue_transcription(session, recording: Recording, *, engine_name: str = "boondock", now: datetime | None = None) -> Job:
    now = now or utcnow()
    existing = (
        session.query(Job)
        .filter_by(kind="transcribe", recording_id=recording.id)
        .filter(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
        .one_or_none()
    )
    if existing:
        return existing
    row = (
        session.query(Transcription)
        .filter_by(recording_id=recording.id, engine=engine_name)
        .one_or_none()
    )
    if row is None:
        row = Transcription(
            recording_id=recording.id,
            engine=engine_name,
            status=TranscriptionStatus.PENDING,
            attempt_count=0,
            requested_at=now,
        )
        session.add(row)
    elif row.status == TranscriptionStatus.DONE:
        return session.query(Job).filter_by(kind="transcribe", recording_id=recording.id).first() or Job(
            kind="transcribe", recording_id=recording.id, status=JobStatus.DONE
        )
    else:
        row.status = TranscriptionStatus.PENDING
        row.error = None
    job = Job(
        kind="transcribe",
        recording_id=recording.id,
        status=JobStatus.PENDING,
        next_run_at=now,
        attempts=row.attempt_count or 0,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job


def _mark_failed(session, job: Job, row: Transcription, message: str, now: datetime) -> str:
    row.status = TranscriptionStatus.FAILED
    row.error = message[:2000]
    row.completed_at = now
    row.next_retry_at = None
    job.status = JobStatus.FAILED
    job.last_error = message[:2000]
    job.next_run_at = None
    job.updated_at = now
    return "failed"


def _schedule_retry(session, job: Job, row: Transcription, message: str, now: datetime, max_attempts: int) -> str:
    row.attempt_count += 1
    job.attempts = row.attempt_count
    row.error = message[:2000]
    job.last_error = message[:2000]
    if row.attempt_count >= max_attempts:
        return _mark_failed(session, job, row, message, now)
    delay = next_backoff_seconds(row.attempt_count)
    when = now + timedelta(seconds=delay)
    row.status = TranscriptionStatus.RETRYING
    row.next_retry_at = when
    job.status = JobStatus.PENDING
    job.next_run_at = when
    job.updated_at = now
    return "retry"


def _s3_url_for(session, recording: Recording, presign) -> str | None:
    s3_row = session.query(S3Object).filter_by(recording_id=recording.id).one_or_none()
    if s3_row is None or not s3_row.key or s3_row.verified_at is None:
        return None
    if presign is None:
        return None
    return presign(s3_row.bucket, s3_row.key)


def process_transcription_job(
    session,
    job: Job,
    *,
    engine,
    data_dir: Path,
    now: datetime | None = None,
    max_attempts: int = 5,
    max_bytes: int = 2 * 1024 * 1024,
    language: str = "",
    api_configured: bool = True,
    presign=None,
) -> str:
    """Advance one job. Returns done, retry, failed, or waiting."""
    now = now or utcnow()
    recording = session.get(Recording, job.recording_id) if job.recording_id else None
    row = (
        session.query(Transcription)
        .filter_by(recording_id=job.recording_id, engine=getattr(engine, "name", "boondock"))
        .one_or_none()
    )
    if recording is None or row is None:
        job.status = JobStatus.FAILED
        job.last_error = "recording or transcription row missing"
        return "failed"
    if recording.storage_state == StorageState.WRITING or not recording.local_path:
        job.next_run_at = now + timedelta(seconds=30)
        return "waiting"
    if not api_configured:
        row.status = TranscriptionStatus.PENDING
        row.error = "Boondock API key is not configured"
        job.status = JobStatus.PENDING
        job.last_error = row.error
        job.next_run_at = now + timedelta(minutes=10)
        job.updated_at = now
        return "waiting"

    path = Path(data_dir) / recording.local_path
    size = recording.size_bytes or (path.stat().st_size if path.is_file() else 0)
    row.status = TranscriptionStatus.RUNNING
    job.status = JobStatus.RUNNING
    session.flush()

    def succeed(text: str) -> str:
        row.status = TranscriptionStatus.DONE
        row.text = text
        row.error = None
        row.completed_at = now
        row.next_retry_at = None
        row.attempt_count = max(row.attempt_count, 1)
        job.status = JobStatus.DONE
        job.last_error = None
        job.next_run_at = None
        job.attempts = row.attempt_count
        job.updated_at = now
        return "done"

    try:
        if size > max_bytes or not path.is_file():
            s3_url = _s3_url_for(session, recording, presign)
            if not s3_url:
                row.attempt_count += 1
                job.attempts = row.attempt_count
                return _mark_failed(
                    session,
                    job,
                    row,
                    "Clip exceeds the 2 MB upload limit and no verified S3 URL is available",
                    now,
                )
            text = engine.transcribe_s3_url(s3_url, language=language)
            row.attempt_count += 1
            return succeed(text)
        try:
            text = engine.transcribe_file(str(path), language=language)
            row.attempt_count += 1
            return succeed(text)
        except TranscriptionError as exc:
            if exc.kind == "too_large" or exc.status_code == 413:
                s3_url = _s3_url_for(session, recording, presign)
                if s3_url:
                    try:
                        text = engine.transcribe_s3_url(s3_url, language=language)
                        row.attempt_count += 1
                        return succeed(text)
                    except TranscriptionError as s3_exc:
                        # The direct upload already cannot succeed. Do not loop on 413.
                        row.attempt_count += 1
                        job.attempts = row.attempt_count
                        return _mark_failed(session, job, row, str(s3_exc), now)
                row.attempt_count += 1
                job.attempts = row.attempt_count
                return _mark_failed(session, job, row, str(exc), now)
            if exc.retryable:
                return _schedule_retry(session, job, row, str(exc), now, max_attempts)
            row.attempt_count += 1
            job.attempts = row.attempt_count
            return _mark_failed(session, job, row, str(exc), now)
    except TranscriptionError as exc:
        if exc.retryable:
            return _schedule_retry(session, job, row, str(exc), now, max_attempts)
        return _mark_failed(session, job, row, str(exc), now)
    except Exception as exc:  # unexpected engine bugs are retryable until the cap
        logger.exception("transcription job %s", job.id)
        return _schedule_retry(session, job, row, str(exc), now, max_attempts)


def run_due_transcriptions(session, *, engine, data_dir: Path, now: datetime | None = None, **kwargs) -> list[str]:
    now = now or utcnow()
    due = (
        session.query(Job)
        .filter(Job.kind == "transcribe", Job.status == JobStatus.PENDING)
        .filter((Job.next_run_at.is_(None)) | (Job.next_run_at <= now))
        .order_by(Job.id.asc())
        .all()
    )
    outcomes = []
    for job in due:
        outcomes.append(
            process_transcription_job(
                session,
                job,
                engine=engine,
                data_dir=data_dir,
                now=now,
                **kwargs,
            )
        )
    return outcomes
