"""Operator-tunable settings live in the database. Secrets stay in the environment."""

from __future__ import annotations

from dataclasses import dataclass

from soundcore.config import Settings
from soundcore.models import Setting, utcnow


TUNABLE_DEFAULTS = (
    "stream_cap",
    "warn_ratio",
    "chunk_seconds",
    "idle_days",
    "free_reserve_ratio",
    "cleanup_used_ratio",
    "timezone",
    "transcribe_enabled",
    "boondock_language",
    "notify_email",
    "notify_webhook",
)


def ensure_settings(session, settings: Settings) -> None:
    defaults = {
        "stream_cap": str(settings.stream_cap),
        "warn_ratio": str(settings.warn_ratio),
        "chunk_seconds": str(settings.chunk_seconds),
        "idle_days": str(settings.idle_days),
        "free_reserve_ratio": str(settings.free_reserve_ratio),
        "cleanup_used_ratio": str(settings.cleanup_used_ratio),
        "timezone": settings.timezone,
        "transcribe_enabled": "1" if settings.transcribe_enabled else "0",
        "boondock_language": settings.boondock_language,
        "notify_email": settings.notify_email,
        "notify_webhook": settings.notify_webhook,
    }
    for key, value in defaults.items():
        if session.get(Setting, key) is None:
            session.add(Setting(key=key, value=value, updated_at=utcnow()))
    session.flush()


def set_setting(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value, updated_at=utcnow()))
    else:
        row.value = value
        row.updated_at = utcnow()
    session.flush()


@dataclass
class Runtime:
    stream_cap: int
    warn_ratio: float
    chunk_seconds: int
    idle_days: int
    free_reserve_ratio: float
    cleanup_used_ratio: float
    timezone: str
    transcribe_enabled: bool
    boondock_language: str
    notify_email: str
    notify_webhook: str
    playback_grace_seconds: int
    transcribe_max_attempts: int
    transcribe_max_bytes: int
    retention_days: int = 0


def _raw(session, key: str, fallback: str) -> str:
    row = session.get(Setting, key)
    if row is None or row.value is None:
        return fallback
    return row.value


def load_runtime(session, settings: Settings) -> Runtime:
    def as_bool(value: str) -> bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}

    return Runtime(
        stream_cap=int(_raw(session, "stream_cap", str(settings.stream_cap)) or settings.stream_cap),
        warn_ratio=float(_raw(session, "warn_ratio", str(settings.warn_ratio)) or settings.warn_ratio),
        chunk_seconds=int(_raw(session, "chunk_seconds", str(settings.chunk_seconds)) or settings.chunk_seconds),
        idle_days=int(_raw(session, "idle_days", str(settings.idle_days)) or settings.idle_days),
        free_reserve_ratio=float(
            _raw(session, "free_reserve_ratio", str(settings.free_reserve_ratio)) or settings.free_reserve_ratio
        ),
        cleanup_used_ratio=float(
            _raw(session, "cleanup_used_ratio", str(settings.cleanup_used_ratio)) or settings.cleanup_used_ratio
        ),
        timezone=_raw(session, "timezone", settings.timezone) or settings.timezone,
        transcribe_enabled=as_bool(_raw(session, "transcribe_enabled", "1" if settings.transcribe_enabled else "0")),
        boondock_language=_raw(session, "boondock_language", settings.boondock_language),
        notify_email=_raw(session, "notify_email", settings.notify_email),
        notify_webhook=_raw(session, "notify_webhook", settings.notify_webhook),
        playback_grace_seconds=settings.playback_grace_seconds,
        transcribe_max_attempts=settings.transcribe_max_attempts,
        transcribe_max_bytes=settings.transcribe_max_bytes,
        retention_days=int(_raw(session, "retention_days", "0") or 0),
    )
