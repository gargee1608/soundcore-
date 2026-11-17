"""Engine and session helpers. SQLite pragmas are applied only for SQLite."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from soundcore.models import Base

_engine = None
_Session: sessionmaker | None = None
_url: str | None = None


def init_engine(database_url: str, *, force: bool = False):
    global _engine, _Session, _url
    if _engine is not None and not force and _url == database_url:
        return _engine
    if _engine is not None:
        _engine.dispose()
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    _engine = engine
    _url = database_url
    _Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_engine():
    if _engine is None:
        raise RuntimeError("Database engine is not initialized")
    return _engine


def create_all() -> None:
    Base.metadata.create_all(get_engine())
    migrate_schema(get_engine())


def migrate_schema(engine=None) -> None:
    """Add channel playback columns in place. Existing rows and other tables stay.

    SQLite only. No DROP, no table rebuild. Legacy channels are marked alsa and
    copy alsa_device into device_id.
    """
    engine = engine or get_engine()
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if "channels" not in tables:
            return
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(channels)")}
        if "audio_backend" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE channels ADD COLUMN audio_backend VARCHAR(16) DEFAULT 'alsa'"
            )
        if "device_id" not in columns:
            conn.exec_driver_sql("ALTER TABLE channels ADD COLUMN device_id VARCHAR(200) DEFAULT ''")
        conn.exec_driver_sql(
            "UPDATE channels SET audio_backend = 'alsa' "
            "WHERE audio_backend IS NULL OR audio_backend = ''"
        )
        conn.exec_driver_sql(
            "UPDATE channels SET device_id = alsa_device "
            "WHERE device_id IS NULL OR device_id = ''"
        )


def session_factory() -> sessionmaker:
    if _Session is None:
        raise RuntimeError("Database engine is not initialized")
    return _Session


def get_session() -> Session:
    return session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
