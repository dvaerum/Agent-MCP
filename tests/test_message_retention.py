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
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets


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
        "(context_key, value, last_updated, updated_by, description) "
        "VALUES (?, ?, ?, ?, ?)",
        ("config_message_retention_days", json.dumps(days), now,
         "test", "retention test"),
    )
    conn.commit()
    conn.close()


def _count_messages() -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS c FROM agent_messages")
    row = cursor.fetchone()
    conn.close()
    return row["c"]


def test_prune_deletes_old_read_messages(client) -> None:
    """Setting retention=1 day should delete read+old messages."""
    from agent_mcp.features.message_retention import prune_old_messages

    now = _dt.datetime.now()
    old_ts = (now - _dt.timedelta(days=5)).isoformat()
    new_ts = now.isoformat()

    old_read = _seed_message("admin", "alice", "old", read=1, timestamp=old_ts)
    new_read = _seed_message("admin", "alice", "new", read=1, timestamp=new_ts)

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


def test_prune_keeps_unread_messages_even_if_old(client) -> None:
    """Unread messages are never pruned (they haven't been seen yet)."""
    from agent_mcp.features.message_retention import prune_old_messages

    old_ts = (_dt.datetime.now() - _dt.timedelta(days=30)).isoformat()
    unread = _seed_message("admin", "alice", "old unread", read=0,
                           timestamp=old_ts)

    _set_retention_days(1)
    prune_old_messages()

    from agent_mcp.db.connection import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT message_id FROM agent_messages WHERE message_id = ?",
                   (unread,))
    row = cursor.fetchone()
    conn.close()
    assert row is not None, "unread message must survive pruning"


def test_prune_no_op_when_retention_unset(client) -> None:
    """No config key -> no pruning."""
    from agent_mcp.features.message_retention import prune_old_messages

    old_ts = (_dt.datetime.now() - _dt.timedelta(days=365)).isoformat()
    _seed_message("admin", "alice", "ancient read", read=1, timestamp=old_ts)

    # Don't set the config key.
    deleted = prune_old_messages()
    assert deleted == 0, "with no retention configured, nothing should be pruned"


def test_prune_no_op_when_retention_zero(client) -> None:
    """Explicit 0 means disabled."""
    from agent_mcp.features.message_retention import prune_old_messages

    old_ts = (_dt.datetime.now() - _dt.timedelta(days=365)).isoformat()
    _seed_message("admin", "alice", "ancient read", read=1, timestamp=old_ts)

    _set_retention_days(0)
    deleted = prune_old_messages()
    assert deleted == 0, "retention=0 should disable pruning"


def test_prune_ignores_bad_config_value(client) -> None:
    """A non-integer or negative value should be treated as disabled,
    not crash the pruner."""
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.features.message_retention import prune_old_messages

    old_ts = (_dt.datetime.now() - _dt.timedelta(days=365)).isoformat()
    _seed_message("admin", "alice", "ancient read", read=1, timestamp=old_ts)

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO project_context "
        "(context_key, value, last_updated, updated_by, description) "
        "VALUES (?, ?, ?, ?, ?)",
        ("config_message_retention_days", json.dumps("not a number"),
         now, "test", "bad value"),
    )
    conn.commit()
    conn.close()

    deleted = prune_old_messages()
    assert deleted == 0


# ---------- background task registration ----------------------------


def test_message_retention_task_is_started_on_startup(client) -> None:
    """The pruner task must be registered as part of start_background_tasks.

    Structural: after the client fixture has run lifespan startup, the
    global scope handle should be populated.
    """
    from agent_mcp.core import globals as g

    assert hasattr(g, "message_retention_task_scope"), (
        "expected globals.message_retention_task_scope handle to exist"
    )
    assert g.message_retention_task_scope is not None, (
        "expected start_background_tasks to register the message-retention "
        "task and store its cancel scope on globals"
    )
