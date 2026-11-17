"""Jobs shared by the worker process and the embedded web process."""

from __future__ import annotations

import logging
import threading

from soundcore.audio.service import _NotifySettings
from soundcore.db import session_scope
from soundcore.models import utcnow
from soundcore.notify import notify
from soundcore.settings_store import load_runtime
from soundcore.storage.s3 import S3Uploader
from soundcore.storage.scheduler import run_midnight_offload, run_storage_guard
from soundcore.transcription.client import build_engine
from soundcore.transcription.worker import run_due_transcriptions

logger = logging.getLogger(__name__)


def _uploader(settings):
    if not settings.s3_configured:
        return None
    return S3Uploader.from_settings(settings)


def _bridge(settings, runtime):
    return _NotifySettings(settings, runtime)


def run_midnight(settings) -> dict:
    with session_scope() as session:
        runtime = load_runtime(session, settings)
        uploader = _uploader(settings)
        summary = run_midnight_offload(
            session,
            uploader,
            settings.data_dir,
            now=utcnow(),
            idle_days=runtime.idle_days,
        )
        if uploader is None and summary["considered"]:
            notify(
                session,
                _bridge(settings, runtime),
                "warning",
                "s3_unconfigured",
                "Midnight offload found idle recordings, but S3 is not configured. Local files were kept.",
                summary,
            )
        elif summary["kept"]:
            notify(
                session,
                _bridge(settings, runtime),
                "warning",
                "s3_offload",
                "Midnight offload kept one or more local files because verification did not succeed.",
                summary,
            )
        else:
            logger.info("midnight offload %s", summary)
        return summary


def run_guard(settings, *, usage=None) -> dict:
    with session_scope() as session:
        runtime = load_runtime(session, settings)

        def _notify(db, level, kind, message, payload=None):
            notify(db, _bridge(settings, runtime), level, kind, message, payload)

        return run_storage_guard(
            session,
            _uploader(settings),
            settings.data_dir,
            now=utcnow(),
            usage=usage,
            used_ratio=runtime.cleanup_used_ratio,
            playback_grace_seconds=runtime.playback_grace_seconds,
            notify=_notify,
        )


def run_transcription_pass(settings) -> list[str]:
    with session_scope() as session:
        runtime = load_runtime(session, settings)
        if not runtime.transcribe_enabled:
            return []
        engine = build_engine(settings)
        presign = None
        uploader = _uploader(settings)
        if uploader is not None:
            presign = lambda bucket, key, _uploader=uploader: _uploader.presign_get(
                key, settings.s3_presign_seconds
            )
        return run_due_transcriptions(
            session,
            engine=engine,
            data_dir=settings.data_dir,
            max_attempts=runtime.transcribe_max_attempts,
            max_bytes=runtime.transcribe_max_bytes,
            language=runtime.boondock_language,
            api_configured=settings.boondock_configured,
            presign=presign,
        )


def start_background(settings, scheduler) -> threading.Event:
    """Interval transcription loop. Scheduler owns midnight and the disk guard."""
    from soundcore.storage.scheduler import register_jobs

    register_jobs(
        scheduler,
        settings.timezone,
        lambda: _safe(run_midnight, settings),
        lambda: _safe(run_guard, settings),
        guard_interval_seconds=settings.guard_interval_seconds,
    )
    scheduler.start()
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            try:
                run_transcription_pass(settings)
            except Exception:
                logger.exception("transcription pass failed")
            stop.wait(5)

    threading.Thread(target=_loop, name="soundcore-transcribe", daemon=True).start()
    return stop


def _safe(fn, settings):
    try:
        fn(settings)
    except Exception:
        logger.exception("%s failed", fn.__name__)
