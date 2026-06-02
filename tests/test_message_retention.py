"""Message retention pruner (Phase 6 follow-up, issue Q).

The agent_messages table grows unbounded — rows are only ever marked
`read=1`, never deleted. This adds a per-project knob:

  project_context["config_message_retention_days"]:
    absent or 0   -> unbounded retention (no pruning)
    positive int N -> delete rows where read=1 AND timestamp older than
                      now() - N days

The pruner runs as a background task once per 24h. This test exercises
the pure SQL operation; a separate test verifies the task is registered
at startup.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). The pruner tests need DB initialised
but never make MCP tool calls; the harness gives us the lifespan +
DB schema with no extra ceremony.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session


# ---------- pruner SQL ----------------------------------------------


def _seed_message(
    sender: str,
    recipient: str,
    content: str,
    read: int,
    timestamp: str,
) -> str:
    """Insert a message directly into the agent_messages table."""
    from agent_mcp.db.connection import get_db_connection

    message_id = f"msg_{secrets.token_hex(8)}"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agent_messages (message_id, sender_id, recipient_id, "
        "message_content, message_type, priority, timestamp, delivered, read) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, sender, recipient, content, "text", "normal",
         timestamp, 1, read),
    )
    conn.commit()
    conn.close()
    return message_id


def _set_retention_days(days: int) -> None:
    """Write config_message_retention_days into project_context."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO project_context "
        "(context_key, value, description, created_at, created_by, "
        "updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("config_message_retention_days", json.dumps(days), "retention test",
         now, "test", now, "test"),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_prune_deletes_old_read_messages(tmp_path) -> None:
    """Setting retention=1 day should delete read+old messages."""
    from agent_mcp.features.message_retention import prune_old_messages

    async with mcp_session(tmp_path):
        now = _dt.datetime.now()
        old_ts = (now - _dt.timedelta(days=5)).isoformat()
        new_ts = now.isoformat()

        old_read = _seed_message("admin", "alice", "old", read=1,
                                 timestamp=old_ts)
        new_read = _seed_message("admin", "alice", "new", read=1,
                                 timestamp=new_ts)

        _set_retention_days(1)

        deleted = prune_old_messages()
        assert deleted == 1, f"expected 1 row pruned, got {deleted}"

        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT message_id FROM agent_messages")
        ids = {r["message_id"] for r in cursor.fetchall()}
        conn.close()

        assert old_read not in ids, "old read message should have been deleted"
        assert new_read in ids, "recent read message should have survived"


@pytest.mark.asyncio
async def test_prune_keeps_unread_messages_even_if_old(tmp_path) -> None:
    """Unread messages are never pruned (they haven't been seen yet)."""
    from agent_mcp.features.message_retention import prune_old_messages

    async with mcp_session(tmp_path):
        old_ts = (_dt.datetime.now() - _dt.timedelta(days=30)).isoformat()
        unread = _seed_message("admin", "alice", "old unread", read=0,
                               timestamp=old_ts)

        _set_retention_days(1)
        prune_old_messages()

        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT message_id FROM agent_messages WHERE message_id = ?",
            (unread,),
        )
        row = cursor.fetchone()
        conn.close()
        assert row is not None, "unread message must survive pruning"


@pytest.mark.asyncio
async def test_prune_no_op_when_retention_unset(tmp_path) -> None:
    """No config key -> no pruning."""
    from agent_mcp.features.message_retention import prune_old_messages

    async with mcp_session(tmp_path):
        old_ts = (_dt.datetime.now() - _dt.timedelta(days=365)).isoformat()
        _seed_message("admin", "alice", "ancient read", read=1, timestamp=old_ts)

        # Don't set the config key.
        deleted = prune_old_messages()
        assert deleted == 0, (
            "with no retention configured, nothing should be pruned"
        )


@pytest.mark.asyncio
async def test_prune_no_op_when_retention_zero(tmp_path) -> None:
    """Explicit 0 means disabled."""
    from agent_mcp.features.message_retention import prune_old_messages

    async with mcp_session(tmp_path):
        old_ts = (_dt.datetime.now() - _dt.timedelta(days=365)).isoformat()
        _seed_message("admin", "alice", "ancient read", read=1, timestamp=old_ts)

        _set_retention_days(0)
        deleted = prune_old_messages()
        assert deleted == 0, "retention=0 should disable pruning"


@pytest.mark.asyncio
async def test_prune_ignores_bad_config_value(tmp_path) -> None:
    """A non-integer or negative value should be treated as disabled,
    not crash the pruner."""
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.features.message_retention import prune_old_messages

    async with mcp_session(tmp_path):
        old_ts = (_dt.datetime.now() - _dt.timedelta(days=365)).isoformat()
        _seed_message("admin", "alice", "ancient read", read=1, timestamp=old_ts)

        now = _dt.datetime.now().isoformat()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO project_context "
            "(context_key, value, description, created_at, created_by, "
            "updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("config_message_retention_days", json.dumps("not a number"),
             "bad value", now, "test", now, "test"),
        )
        conn.commit()
        conn.close()

        deleted = prune_old_messages()
        assert deleted == 0


# ---------- background task registration ----------------------------


def test_message_retention_task_is_registered_by_start_background_tasks() -> None:
    """The pruner task must be registered by start_background_tasks.

    The lifespan doesn't itself spawn background tasks (the CLI runner
    does, with an anyio task group). And `task_group.start()` returns
    whatever the task's `task_status.started()` reports, which is
    `None` everywhere in this codebase — so we can't observe the cancel
    scope on globals. Instead, verify the call site directly: the
    pruner is imported into server_lifecycle and start_background_tasks
    invokes it on the task group.

    This is a static-source assertion — no app instance required, so
    no harness session needed.
    """
    import inspect

    from agent_mcp.app import server_lifecycle
    from agent_mcp.core import globals as g
    from agent_mcp.features import message_retention

    # The global handle is declared even if its runtime value stays None
    # (parity with the other background-task globals).
    assert hasattr(g, "message_retention_task_scope"), (
        "expected globals.message_retention_task_scope handle to exist"
    )

    # The lifecycle module must reference the pruner so it's imported
    # at startup and registered with the task group.
    assert hasattr(server_lifecycle, "run_message_retention_periodically"), (
        "expected server_lifecycle to import run_message_retention_periodically"
    )
    assert (
        server_lifecycle.run_message_retention_periodically
        is message_retention.run_message_retention_periodically
    ), (
        "expected the imported symbol to be the pruner from "
        "features.message_retention"
    )

    src = inspect.getsource(server_lifecycle.start_background_tasks)
    assert "run_message_retention_periodically" in src, (
        "expected start_background_tasks to launch the message-retention "
        "pruner on the task group"
    )
    assert "task_group.start" in src or "task_group.start_soon" in src, (
        "expected start_background_tasks to actually start the task"
    )
