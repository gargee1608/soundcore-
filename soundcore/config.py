"""Runtime configuration from the environment and an optional JSON file.

Environment variables win over the file. The admin password defaults to
``C1nb3rr1`` so a new install can sign in. API keys and cloud credentials
still have no usable default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


DEFAULT_ADMIN_PASSWORD = "C1nb3rr1"


def local_timezone_name() -> str:
    from datetime import datetime

    tz = datetime.now().astimezone().tzinfo
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    return "UTC"


@dataclass
class Settings:
    secret_key: str
    database_url: str
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8741
    behind_proxy: bool = False
    cookie_secure: bool = False
    admin_email: str = ""
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    reset_admin: bool = False
    audio_backend: str = "auto"
    embed_worker: bool = True
    seed_demo: bool = False
    timezone: str = "UTC"
    stream_cap: int = 8
    warn_ratio: float = 0.8
    chunk_seconds: int = 45
    default_sample_rate: int = 16000
    idle_days: int = 7
    free_reserve_ratio: float = 0.05
    cleanup_used_ratio: float = 0.95
    playback_grace_seconds: int = 120
    s3_bucket: str = ""
    s3_prefix: str = "soundcore"
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    s3_presign_seconds: int = 3600
    boondock_base_url: str = "https://api.boondock.cloud"
    boondock_api_key: str = ""
    boondock_timeout: float = 60.0
    boondock_language: str = ""
    boondock_model: str = ""
    transcribe_max_bytes: int = 2 * 1024 * 1024
    transcribe_max_attempts: int = 5
    transcribe_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True
    notify_email: str = ""
    notify_webhook: str = ""
    live_pcm_host: str = "127.0.0.1"
    live_pcm_port: int = 8742
    guard_interval_seconds: int = 60
    allowed_sample_rates: tuple[int, ...] = field(
        default_factory=lambda: (8000, 16000, 22050, 44100, 48000)
    )

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def s3_configured(self) -> bool:
        return bool(self.s3_bucket and self.aws_access_key_id and self.aws_secret_access_key)

    @property
    def boondock_configured(self) -> bool:
        return bool(self.boondock_api_key and self.boondock_base_url)


def _read_config_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must be a JSON object")
    return data


def _pick(env: dict, file_data: dict, key: str, env_name: str, default=None):
    if env_name in env and env[env_name] != "":
        return env[env_name]
    if key in file_data and file_data[key] not in (None, ""):
        return file_data[key]
    return default


def load_settings(*, dotenv: bool = False, environ: dict | None = None) -> Settings:
    if dotenv:
        load_dotenv(override=False)
    env = dict(os.environ if environ is None else environ)
    config_path = Path(env.get("SOUNDCORE_CONFIG") or "config/soundcore.json")
    file_data: dict = {}
    if config_path.is_file():
        file_data = _read_config_file(config_path)

    def raw(key: str, env_name: str, default=None):
        return _pick(env, file_data, key, env_name, default)

    data_dir = Path(str(raw("data_dir", "SOUNDCORE_DATA_DIR", "data"))).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "db").mkdir(parents=True, exist_ok=True)
    (data_dir / "recordings").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)

    secret = str(raw("secret_key", "SOUNDCORE_SECRET_KEY", "") or "")
    if not secret:
        secret_path = data_dir / ".secret_key"
        if secret_path.is_file():
            secret = secret_path.read_text(encoding="utf-8").strip()
        else:
            import secrets

            secret = secrets.token_hex(32)
            secret_path.write_text(secret, encoding="utf-8")
            try:
                secret_path.chmod(0o600)
            except OSError:
                pass

    db_url = str(raw("database_url", "SOUNDCORE_DATABASE_URL", "") or "")
    if not db_url:
        db_path = (data_dir / "db" / "soundcore.db").resolve()
        db_url = f"sqlite:///{db_path}"

    tz = str(raw("timezone", "SOUNDCORE_TIMEZONE", "") or "") or local_timezone_name()
    api_key = str(raw("boondock_api_key", "SOUNDCORE_BOONDOCK_API_KEY", "") or "")

    return Settings(
        secret_key=secret,
        database_url=db_url,
        data_dir=data_dir,
        host=str(raw("host", "SOUNDCORE_HOST", "127.0.0.1")),
        port=_int(str(raw("port", "SOUNDCORE_PORT", "8741")), 8741),
        behind_proxy=_bool(str(raw("behind_proxy", "SOUNDCORE_BEHIND_PROXY", "")) or None, False),
        cookie_secure=_bool(str(raw("cookie_secure", "SOUNDCORE_COOKIE_SECURE", "")) or None, False),
        admin_email=str(raw("admin_email", "SOUNDCORE_ADMIN_EMAIL", "") or ""),
        admin_password=str(
            raw("admin_password", "SOUNDCORE_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
            or DEFAULT_ADMIN_PASSWORD
        ),
        reset_admin=_bool(str(raw("reset_admin", "SOUNDCORE_RESET_ADMIN", "")) or None, False),
        audio_backend=str(raw("audio_backend", "SOUNDCORE_AUDIO_BACKEND", "auto") or "auto"),
        embed_worker=_bool(str(raw("embed_worker", "SOUNDCORE_EMBED_WORKER", "")) or None, True),
        seed_demo=_bool(str(raw("seed_demo", "SOUNDCORE_SEED_DEMO", "")) or None, False),
        timezone=tz,
        stream_cap=_int(str(raw("stream_cap", "SOUNDCORE_STREAM_CAP", "8")), 8),
        warn_ratio=_float(str(raw("warn_ratio", "SOUNDCORE_WARN_RATIO", "0.8")), 0.8),
        chunk_seconds=_int(str(raw("chunk_seconds", "SOUNDCORE_CHUNK_SECONDS", "45")), 45),
        default_sample_rate=_int(str(raw("default_sample_rate", "SOUNDCORE_SAMPLE_RATE", "16000")), 16000),
        idle_days=_int(str(raw("idle_days", "SOUNDCORE_IDLE_DAYS", "7")), 7),
        free_reserve_ratio=_float(str(raw("free_reserve_ratio", "SOUNDCORE_FREE_RESERVE", "0.05")), 0.05),
        cleanup_used_ratio=_float(str(raw("cleanup_used_ratio", "SOUNDCORE_CLEANUP_USED", "0.95")), 0.95),
        playback_grace_seconds=_int(str(raw("playback_grace_seconds", "SOUNDCORE_PLAYBACK_GRACE", "120")), 120),
        s3_bucket=str(raw("s3_bucket", "SOUNDCORE_S3_BUCKET", "") or ""),
        s3_prefix=str(raw("s3_prefix", "SOUNDCORE_S3_PREFIX", "soundcore") or "soundcore"),
        s3_region=str(raw("s3_region", "SOUNDCORE_S3_REGION", "us-east-1") or "us-east-1"),
        s3_endpoint_url=str(raw("s3_endpoint_url", "SOUNDCORE_S3_ENDPOINT_URL", "") or ""),
        aws_access_key_id=str(raw("aws_access_key_id", "AWS_ACCESS_KEY_ID", "") or ""),
        aws_secret_access_key=str(raw("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "") or ""),
        s3_presign_seconds=_int(str(raw("s3_presign_seconds", "SOUNDCORE_S3_PRESIGN_SECONDS", "3600")), 3600),
        boondock_base_url=str(
            raw("boondock_base_url", "SOUNDCORE_BOONDOCK_BASE_URL", "https://api.boondock.cloud")
            or "https://api.boondock.cloud"
        ),
        boondock_api_key=api_key,
        boondock_timeout=_float(str(raw("boondock_timeout", "SOUNDCORE_BOONDOCK_TIMEOUT", "60")), 60.0),
        boondock_language=str(raw("boondock_language", "SOUNDCORE_BOONDOCK_LANGUAGE", "") or ""),
        boondock_model=str(raw("boondock_model", "SOUNDCORE_BOONDOCK_MODEL", "") or ""),
        transcribe_max_bytes=_int(
            str(raw("transcribe_max_bytes", "SOUNDCORE_TRANSCRIBE_MAX_BYTES", str(2 * 1024 * 1024))),
            2 * 1024 * 1024,
        ),
        transcribe_max_attempts=_int(
            str(raw("transcribe_max_attempts", "SOUNDCORE_TRANSCRIBE_MAX_ATTEMPTS", "5")), 5
        ),
        transcribe_enabled=_bool(
            str(raw("transcribe_enabled", "SOUNDCORE_TRANSCRIBE_ENABLED", "")) or None,
            bool(api_key),
        ),
        smtp_host=str(raw("smtp_host", "SOUNDCORE_SMTP_HOST", "") or ""),
        smtp_port=_int(str(raw("smtp_port", "SOUNDCORE_SMTP_PORT", "587")), 587),
        smtp_user=str(raw("smtp_user", "SOUNDCORE_SMTP_USER", "") or ""),
        smtp_password=str(raw("smtp_password", "SOUNDCORE_SMTP_PASSWORD", "") or ""),
        smtp_from=str(raw("smtp_from", "SOUNDCORE_SMTP_FROM", "") or ""),
        smtp_tls=_bool(str(raw("smtp_tls", "SOUNDCORE_SMTP_TLS", "")) or None, True),
        notify_email=str(raw("notify_email", "SOUNDCORE_NOTIFY_EMAIL", "") or ""),
        notify_webhook=str(raw("notify_webhook", "SOUNDCORE_NOTIFY_WEBHOOK", "") or ""),
        live_pcm_host=str(raw("live_pcm_host", "SOUNDCORE_LIVE_HOST", "127.0.0.1") or "127.0.0.1"),
        live_pcm_port=_int(str(raw("live_pcm_port", "SOUNDCORE_LIVE_PORT", "8742")), 8742),
        guard_interval_seconds=_int(str(raw("guard_interval_seconds", "SOUNDCORE_GUARD_INTERVAL", "60")), 60),
    )
