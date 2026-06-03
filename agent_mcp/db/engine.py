# Agent-MCP/agent_mcp/db/engine.py
"""SQLAlchemy engine + session factory for agent-mcp.

Agent-mcp is single-DB-per-project: the on-disk file lives at
`<project_dir>/.agent/mcp_state.db` and `get_db_path()` already
resolves that path from the `MCP_PROJECT_DIR` environment variable.

This module wraps that file in a SQLAlchemy `Engine` + scoped
`Session`. Engines are cached per URL so we don't pay the
`create_engine` cost (and don't open extra fd's against the same
sqlite file) on every call.

Phase 7a adopts SQLAlchemy table-by-table; for now only the
`ProjectContext` model uses this surface. The raw-SQL connection in
`agent_mcp.db.connection` keeps serving the rest of the schema until
later phases migrate each table.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..core.config import get_db_path, logger


class Base(DeclarativeBase):
    """Shared declarative base for every agent-mcp ORM model."""


# Engines are keyed by sqlite URL. In production there's exactly one
# project per process, so the cache is single-entry; tests using
# `tmp_path` fixtures get one entry per project_dir.
_engines: dict[str, Engine] = {}
_engines_lock = threading.Lock()


def _make_engine(url: str) -> Engine:
    """Build a fresh SQLAlchemy engine for the given sqlite URL.

    Mirrors the connection pragmas that raw-SQL `get_db_connection()`
    sets: WAL journal + foreign keys ON. Doing it here too keeps
    behavior consistent regardless of which surface opens the first
    connection.
    """
    engine = create_engine(
        url,
        future=True,
        # check_same_thread=False matches the raw-SQL connection: the
        # write queue + Starlette threadpool both touch the DB from
        # multiple threads.
        connect_args={"check_same_thread": False, "timeout": 10.0},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        # Keep in sync with the PRAGMA block in
        # `agent_mcp/db/connection.py::get_db_connection` — the raw-SQL
        # and SQLAlchemy surfaces must agree on these per the 2026-06-02
        # database review (busy_timeout, synchronous, cache_size,
        # mmap_size, temp_store).
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-20000")
            cursor.execute("PRAGMA mmap_size=268435456")
            cursor.execute("PRAGMA temp_store=MEMORY")
        finally:
            cursor.close()

    return engine


def _db_url_for_path(db_path: str) -> str:
    return f"sqlite:///{db_path}"


def get_engine() -> Engine:
    """Return the SQLAlchemy engine for the current project's DB.

    Resolves the project DB path lazily via `get_db_path()`, so this
    works the same as the raw-SQL connection helper — it relies on
    `MCP_PROJECT_DIR` being set by the CLI/lifespan startup.
    """
    db_path = str(get_db_path())
    # Ensure the directory exists; mirrors get_db_connection().
    try:
        get_db_path().parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(
            f"Failed to create directory for SQLAlchemy engine at "
            f"{get_db_path().parent}: {e}"
        )
        raise

    url = _db_url_for_path(db_path)
    with _engines_lock:
        engine = _engines.get(url)
        if engine is None:
            engine = _make_engine(url)
            _engines[url] = engine
    return engine


def _make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


# Per-engine session factory cache.
_session_factories: dict[int, sessionmaker[Session]] = {}


def SessionLocal() -> Session:
    """Return a new SQLAlchemy `Session` bound to the current engine.

    Named `SessionLocal` (PEP 8 violation intentional) to match the
    SQLAlchemy 2.x tutorial convention so the API is immediately
    recognisable to anyone familiar with the docs.
    """
    engine = get_engine()
    factory = _session_factories.get(id(engine))
    if factory is None:
        factory = _make_session_factory(engine)
        _session_factories[id(engine)] = factory
    return factory()


@contextmanager
def get_session() -> Iterator[Session]:
    """Context-managed SQLAlchemy session.

    Commits on normal exit, rolls back on exception, always closes.
    The recommended way to use the ORM from tool/route code:

        with get_session() as session:
            row = session.query(ProjectContext).filter(...).one_or_none()
            ...

    Note: callers that want explicit transaction control (e.g. the
    bulk_update path that wants to roll back inside the `with`) can
    instead use `SessionLocal()` directly.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Drop all cached engines + session factories.

    Used by tests that swap `MCP_PROJECT_DIR` between cases. Each test
    gets a fresh project_dir + DB path, so the cached engine bound to
    the previous one would point at the wrong file.
    """
    with _engines_lock:
        for engine in _engines.values():
            try:
                engine.dispose()
            except Exception:  # pragma: no cover — defensive
                pass
        _engines.clear()
        _session_factories.clear()
