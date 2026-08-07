"""Wave 4 (cleanup/wave-4-delete-admin-pseudo-agent) — the admin
pseudo-agent row is gone.

Background
----------
PR #100 (migration 0008) introduced the synthetic
``agent_id='admin'`` row in the ``agents`` table so the three
deferred FK constraints from the 2026-06-02 DB review could be
declared:

  * ``agent_messages.sender_id`` → ``agents.agent_id``
  * ``agent_messages.recipient_id`` → ``agents.agent_id``
  * ``mcp_sessions.agent_id`` → ``agents.agent_id``

Wave 4 of the admin_token retirement (this PR) closes the loop the
other way: the synthetic row is deleted by migration 0014, and the
FK constraints that pinned it in place (including the two nullable
FKs from 0007 — ``tasks.assigned_to`` and
``claude_code_sessions.agent_id``) are dropped via the same
``batch_alter_table`` rebuild mechanism that created them.

Post-Wave-4 contract this file pins:

  * The ``agents`` table contains zero rows with
    ``agent_id='admin'`` after a fresh DB init.
  * The five FK constraints listed above are no longer declared on
    their respective tables.
  * Writes that previously required the admin parent row
    (``agent_messages`` with ``sender_id='admin'`` or
    ``recipient_id='admin'``; ``mcp_sessions`` with
    ``agent_id='admin'``) succeed without any agents-table parent —
    those columns are now durable labels, not relational pointers.
  * ``PRAGMA foreign_key_check`` is clean after startup (no orphan
    rows from the FK drop).
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# The FKs migration 0014 drops. Format: (table, column, ref_table, ref_col).
_DROPPED_FKS = [
    ("agent_messages", "sender_id", "agents", "agent_id"),
    ("agent_messages", "recipient_id", "agents", "agent_id"),
    ("mcp_sessions", "agent_id", "agents", "agent_id"),
    ("tasks", "assigned_to", "agents", "agent_id"),
    ("claude_code_sessions", "agent_id", "agents", "agent_id"),
]


def _fk_list(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Return PRAGMA foreign_key_list(table) as (col, ref_table, ref_col)."""
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    return [(r[3], r[2], r[4]) for r in rows]


async def test_fresh_project_has_no_admin_pseudo_agent(tmp_path) -> None:
    """Wave 4: a freshly-initialised project — i.e. after migrations
    run but BEFORE any application-side row inserts — must have zero
    rows in ``agents`` with ``agent_id='admin'``.

    retire-system-token Wave 1 (the harness's principal is now a real
    per-agent row with ``agent_id='admin'``) means we can't query
    inside an open ``mcp_session``: the harness re-inserts the row
    immediately after migrations run, masking the assertion. Run
    migrations via a barebones startup path that DOESN'T go through
    the harness, then query the freshly-migrated DB."""
    from agent_mcp.db.schema import init_database

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    import os
    os.environ["MCP_PROJECT_DIR"] = str(project_dir)
    try:
        # init_database runs the Alembic upgrade chain (which includes
        # 0014's DELETE of the synthetic admin row) without going
        # through application_startup / the harness.
        init_database()
        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE agent_id = 'admin'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0, (
            f"Wave 4 deleted the admin pseudo-agent — expected zero "
            f"rows, found {count}"
        )
    finally:
        os.environ.pop("MCP_PROJECT_DIR", None)


async def test_admin_targeting_fks_are_dropped(tmp_path) -> None:
    """The five FKs that targeted agents.agent_id and required the
    pseudo-agent row must be gone after migration 0014."""
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            for table, col, ref_table, ref_col in _DROPPED_FKS:
                fks = _fk_list(conn, table)
                assert (col, ref_table, ref_col) not in fks, (
                    f"FK {table}.{col} -> {ref_table}.{ref_col} should have "
                    f"been dropped by migration 0014, still present in: {fks}"
                )
        finally:
            conn.close()


async def test_agent_messages_with_admin_sender_succeeds_without_parent(
    tmp_path,
) -> None:
    """Inserting an agent_messages row with ``sender_id='admin'`` and
    ``recipient_id='admin'`` must succeed without any agents-table
    parent row — the FK is gone, the columns are now durable labels.

    retire-system-token Wave 1: the harness re-seeds an admin row for
    its principal; DELETE that row first so we exercise the
    no-parent-row case the migration's FK-drop is meant to allow."""
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        conn = get_db_connection()
        try:
            # Wipe the harness's admin row so the test exercises the
            # post-Wave-4 "label without parent" path the migration
            # is meant to allow.
            conn.execute("DELETE FROM agents WHERE agent_id='admin'")
            conn.commit()
            row = conn.execute(
                "SELECT 1 FROM agents WHERE agent_id='admin'"
            ).fetchone()
            assert row is None
            conn.execute(
                "INSERT INTO agent_messages "
                "(message_id, sender_id, recipient_id, message_content, "
                " timestamp) "
                "VALUES ('m-admin', 'admin', 'admin', 'hello', 't')"
            )
            conn.commit()
            content = conn.execute(
                "SELECT message_content FROM agent_messages "
                "WHERE message_id='m-admin'"
            ).fetchone()
            assert content is not None and content[0] == "hello"
        finally:
            conn.close()


async def test_mcp_sessions_with_admin_agent_id_succeeds_without_parent(
    tmp_path,
) -> None:
    """Inserting an mcp_sessions row with ``agent_id='admin'`` must
    succeed without any agents-table parent row.

    This is the dashboard's GET /mcp open path: the cookie-injected
    system bearer resolves to ``agent_id='admin'`` inside the
    backend's ``session_registry.register_session`` call. With the
    FK gone, that no longer needs a synthetic parent.

    retire-system-token Wave 1: harness re-seeds the admin row; we
    DELETE it first to exercise the FK-gone path."""
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        conn = get_db_connection()
        try:
            conn.execute("DELETE FROM agents WHERE agent_id='admin'")
            conn.commit()
            conn.execute(
                "INSERT INTO mcp_sessions "
                "(session_id, agent_id, opened_at, last_seen_at, "
                " bearer_token_hash) "
                "VALUES ('s-admin', 'admin', 't', 't', 'h')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT agent_id FROM mcp_sessions WHERE session_id='s-admin'"
            ).fetchone()
            assert row is not None and row[0] == "admin"
        finally:
            conn.close()


async def test_pragma_foreign_key_check_clean_after_startup(tmp_path) -> None:
    """``PRAGMA foreign_key_check`` must return no rows after startup.

    The migration drops several FKs via batch_alter_table; if the
    rebuild left any orphan or the env.py post-commit safety net
    surfaces unexpected rows, this catches it."""
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()
        assert rows == [], f"foreign_key_check returned orphans: {rows}"


async def test_other_fks_survive_the_migration(tmp_path) -> None:
    """Wave 4 drops *only* the five admin-targeting FKs. Other FKs
    on the rebuilt tables — particularly
    ``agent_messages.parent_message_id -> agent_messages.message_id``
    (migration 0012) and ``tasks.parent_task -> tasks.task_id``
    (migration 0007) — must survive the rebuild.

    Regression guard: a misconfigured batch_alter_table that doesn't
    re-emit the surviving FKs would silently strip them."""
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            tasks_fks = _fk_list(conn, "tasks")
            assert ("parent_task", "tasks", "task_id") in tasks_fks, (
                f"tasks.parent_task -> tasks.task_id FK was lost in the "
                f"Wave 4 rebuild; got {tasks_fks}"
            )
            msg_fks = _fk_list(conn, "agent_messages")
            assert ("parent_message_id", "agent_messages", "message_id") in msg_fks, (
                f"agent_messages.parent_message_id -> agent_messages."
                f"message_id FK was lost in the Wave 4 rebuild; got "
                f"{msg_fks}"
            )
        finally:
            conn.close()
