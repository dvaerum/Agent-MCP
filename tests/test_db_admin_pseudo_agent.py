"""Test suite for PR-G1 — admin pseudo-agent + the 3 deferred FKs.

PR #96 (migration 0007) shipped 4 of the 7 implicit FK constraints
identified in the 2026-06-02 database review and deferred 3 others
because they reference `agents.agent_id` and the `admin` identity
has no row in `agents` — admin was previously enforced via
`g.admin_token` alone.

Migration 0008 closes that gap:

  * Adds a synthetic `admin` row to `agents` (INSERT OR IGNORE).
  * Drops/recreates any orphan rows on the three deferred columns.
  * Adds the three deferred FK constraints.

`application_startup` also re-inserts the admin row at every boot
for defence in depth, so an operator who wipes the row recovers on
the next restart.

Tests cover:
  * The synthetic admin row is present after lifespan startup.
  * The three deferred FK constraints are now declared.
  * FK violations on agent_messages / mcp_sessions are rejected.
  * Inserting agent_messages WITH sender_id='admin' succeeds (the
    FK is satisfied by the synthetic row).
  * `PRAGMA foreign_key_check` returns no rows after startup.
  * The lifespan-startup helper is idempotent on its own.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# The three FKs we ship in PR-G1. Format mirrors test_db_foreign_keys.py.
_NEWLY_REQUIRED_FKS = [
    ("agent_messages", "sender_id", "agents", "agent_id"),
    ("agent_messages", "recipient_id", "agents", "agent_id"),
    ("mcp_sessions", "agent_id", "agents", "agent_id"),
]


def _fk_list(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Return PRAGMA foreign_key_list(table) as (col, ref_table, ref_col)."""
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    return [(r[3], r[2], r[4]) for r in rows]


async def test_admin_pseudo_agent_row_present_after_startup(tmp_path) -> None:
    """Lifespan startup must seed the synthetic admin row."""
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            row = conn.execute(
                "SELECT agent_id, status FROM agents WHERE agent_id = 'admin'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "admin pseudo-agent row missing after startup"
        assert row[0] == "admin"
        assert row[1] == "system"


async def test_deferred_fks_now_declared(tmp_path) -> None:
    """The three deferred FK constraints are present after migration 0008."""
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            for table, col, ref_table, ref_col in _NEWLY_REQUIRED_FKS:
                fks = _fk_list(conn, table)
                assert (col, ref_table, ref_col) in fks, (
                    f"missing FK {table}.{col} -> {ref_table}.{ref_col}; "
                    f"have {fks}"
                )
        finally:
            conn.close()


async def test_fk_violation_on_agent_messages_rejected(tmp_path) -> None:
    """Inserting agent_messages with a nonexistent sender_id must fail."""
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        conn = get_db_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO agent_messages "
                    "(message_id, sender_id, recipient_id, message_content, "
                    " timestamp) "
                    "VALUES ('m1', 'no-such-agent', 'admin', 'x', 't')"
                )
        finally:
            conn.close()


async def test_fk_violation_on_mcp_sessions_rejected(tmp_path) -> None:
    """Inserting mcp_sessions with a nonexistent agent_id must fail."""
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        conn = get_db_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO mcp_sessions "
                    "(session_id, agent_id, opened_at, last_seen_at, "
                    " bearer_token_hash) "
                    "VALUES ('s1', 'no-such-agent', 't', 't', 'h')"
                )
        finally:
            conn.close()


async def test_agent_messages_with_admin_sender_succeeds(tmp_path) -> None:
    """Inserting agent_messages with sender_id='admin' succeeds.

    Proves the synthetic admin row actually satisfies the FK — this
    is the whole point of PR-G1.
    """
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO agent_messages "
                "(message_id, sender_id, recipient_id, message_content, "
                " timestamp) "
                "VALUES ('m-admin', 'admin', 'admin', 'hello', 't')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT message_content FROM agent_messages "
                "WHERE message_id='m-admin'"
            ).fetchone()
            assert row is not None and row[0] == "hello"
        finally:
            conn.close()


async def test_pragma_foreign_key_check_clean_after_startup(tmp_path) -> None:
    """`PRAGMA foreign_key_check` must return no rows after startup.

    Anything else means the migration left orphans behind that violate
    one of the now-declared FKs — that's a deploy-blocker.
    """
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()
        assert rows == [], f"foreign_key_check returned orphans: {rows}"


async def test_ensure_admin_pseudo_agent_row_is_idempotent(tmp_path) -> None:
    """The lifespan-startup helper must be safe to call repeatedly."""
    from agent_mcp.app.server_lifecycle import _ensure_admin_pseudo_agent_row
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        # Lifespan already called it once. Call a few more times.
        _ensure_admin_pseudo_agent_row()
        _ensure_admin_pseudo_agent_row()
        _ensure_admin_pseudo_agent_row()

        conn = sqlite3.connect(str(get_db_path()))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE agent_id = 'admin'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, f"expected exactly 1 admin row, got {count}"
