"""Test suite for PR-1 of the database review improvements.

Covers:
- Connection-level PRAGMAs (item 3 of the review): busy_timeout,
  synchronous, cache_size, mmap_size, temp_store applied on every
  raw-SQL `get_db_connection()` and every SQLAlchemy engine.
- Composite + single-column indexes (items 1 + 7) created by
  Alembic migration 0006_db_review_indexes_and_init_mcp_sessions.
- mcp_sessions table parity (item 15): `init_database()` creates the
  table so a fresh DB doesn't depend on Alembic having run.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Item 3: connection-level PRAGMAs
# ---------------------------------------------------------------------------


_EXPECTED_PRAGMAS = {
    "journal_mode": "wal",
    "foreign_keys": 1,
    "busy_timeout": 5000,
    "synchronous": 1,           # NORMAL = 1
    "cache_size": -20000,        # negative = KiB
    "mmap_size": 268435456,      # 256 MiB
    "temp_store": 2,             # MEMORY = 2
}


def _read_pragma(conn: sqlite3.Connection, name: str):
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


async def test_raw_connection_applies_all_pragmas(tmp_path) -> None:
    """`get_db_connection()` must set every PRAGMA listed in the review."""
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        conn = get_db_connection()
        try:
            for name, expected in _EXPECTED_PRAGMAS.items():
                actual = _read_pragma(conn, name)
                if name == "journal_mode":
                    assert str(actual).lower() == expected, (
                        f"PRAGMA {name}: expected {expected}, got {actual}"
                    )
                else:
                    assert actual == expected, (
                        f"PRAGMA {name}: expected {expected}, got {actual}"
                    )
        finally:
            conn.close()


async def test_sqlalchemy_engine_applies_all_pragmas(tmp_path) -> None:
    """SQLAlchemy `connect` event must mirror the raw-SQL PRAGMA block."""
    from agent_mcp.db.engine import get_engine

    async with mcp_session(tmp_path):
        engine = get_engine()
        with engine.connect() as conn:
            raw = conn.connection.dbapi_connection  # type: ignore[union-attr]
            for name, expected in _EXPECTED_PRAGMAS.items():
                actual = _read_pragma(raw, name)
                if name == "journal_mode":
                    assert str(actual).lower() == expected, (
                        f"engine PRAGMA {name}: expected {expected}, got {actual}"
                    )
                else:
                    assert actual == expected, (
                        f"engine PRAGMA {name}: expected {expected}, got {actual}"
                    )


# ---------------------------------------------------------------------------
# Items 1 + 7: indexes from the Alembic migration
# ---------------------------------------------------------------------------


_REQUIRED_INDEXES = {
    "tasks": {
        # Item 1 (critical) — the wait_for_events composite.
        "idx_tasks_assigned_to_updated_at",
        # Item 7 — hot single-column filters.
        "idx_tasks_status",
        "idx_tasks_priority",
    },
    "agent_messages": {
        # Item 7.
        "idx_agent_messages_delivered",
    },
    "claude_code_sessions": {
        # Item 7.
        "idx_claude_sessions_status",
    },
}


async def test_review_indexes_present_after_startup(tmp_path) -> None:
    """All indexes the review calls out are created at startup."""
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            for table, expected in _REQUIRED_INDEXES.items():
                rows = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name=?",
                    (table,),
                ).fetchall()
                names = {r[0] for r in rows}
                missing = expected - names
                assert not missing, (
                    f"table {table}: missing indexes {missing}; have {names}"
                )
        finally:
            conn.close()


async def test_tasks_composite_index_uses_descending_updated_at(tmp_path) -> None:
    """The critical composite must be on `(assigned_to, updated_at DESC)`.

    SQLite stores the index DDL verbatim in sqlite_master.sql; we look
    for the DESC keyword to confirm the seek-direction matches the
    query pattern in `wait_for_events`.
    """
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='idx_tasks_assigned_to_updated_at'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "composite tasks index missing"
        ddl = (row[0] or "").lower()
        assert "assigned_to" in ddl
        assert "updated_at" in ddl
        assert "desc" in ddl, f"expected DESC sort in index DDL, got: {ddl!r}"


# ---------------------------------------------------------------------------
# Item 15: init_database() creates mcp_sessions defensively
# ---------------------------------------------------------------------------


async def test_init_database_creates_mcp_sessions_without_alembic(
    tmp_path, monkeypatch
) -> None:
    """`init_database()` must create `mcp_sessions` even when Alembic
    hasn't run yet — the migration remains the canonical source but
    fresh-DB parity prevents startup races and aids manual DB
    bootstrapping.
    """
    project_dir = tmp_path / "no-alembic-project"
    project_dir.mkdir()
    monkeypatch.setenv("MCP_PROJECT_DIR", str(project_dir))

    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.db.schema import init_database

    init_database()
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='mcp_sessions'"
        ).fetchall()
        assert rows, "init_database() did not create mcp_sessions"

        # Indexes follow the migration: agent + last_seen_at.
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='mcp_sessions'"
        ).fetchall()
        idx_names = {r[0] for r in idx_rows}
        assert "idx_mcp_sessions_agent" in idx_names
        assert "idx_mcp_sessions_last_seen" in idx_names
    finally:
        conn.close()
