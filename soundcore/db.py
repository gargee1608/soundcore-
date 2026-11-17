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
