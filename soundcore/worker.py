"""Capture, offload, and transcription process: `python -m soundcore.worker`."""

from __future__ import annotations

import logging
import signal
import threading

from soundcore.audio.detect import get_backend
from soundcore.audio.livestream import UdpPublisher
from soundcore.audio.service import AudioService
from soundcore.auth import ensure_admin
from soundcore.config import load_settings
from soundcore.db import create_all, init_engine, session_scope
from soundcore.maintenance import start_background
from soundcore.seed import seed_channels
from soundcore.settings_store import ensure_settings
from soundcore.storage.scheduler import build_scheduler

logger = logging.getLogger(__name__)


def main() -> None:
    settings = load_settings(dotenv=True)
    from soundcore.app import configure_logging

    configure_logging(settings.log_dir)
    init_engine(settings.database_url)
    create_all()
    backend = get_backend(settings.audio_backend)
    with session_scope() as db:
        ensure_admin(db, settings.admin_email, settings.admin_password, reset=settings.reset_admin)
        ensure_settings(db, settings)
        seed_channels(
            db,
            backend,
            enable_demo=settings.seed_demo,
            cap=settings.stream_cap,
            sample_rate=settings.default_sample_rate,
        )
    publisher = UdpPublisher(settings.live_pcm_host, settings.live_pcm_port)
    audio = AudioService(settings, backend, publisher)
    audio.start()
    scheduler = build_scheduler(settings.timezone)
    stop = start_background(settings, scheduler)
    halt = threading.Event()

    def _shutdown(*_args):
        halt.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    logger.info("SoundCore worker running (backend=%s)", backend.name)
    halt.wait()
    stop.set()
    audio.stop()
    scheduler.shutdown(wait=False)
    publisher.close()


if __name__ == "__main__":
    main()