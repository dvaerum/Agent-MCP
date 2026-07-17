"""BL-R21-1: the event-feed truncation clamp (BL-R20-1) must be
PROPAGATED to the persisted cursor by every caller.

BL-R20-1 made ``_collect_events_for`` fetch messages oldest-first
(``timestamp ASC LIMIT 500``) and, when the batch fills the cap, clamp
its OWN events to the message-boundary ``msg_cap_ts`` so the cursor
never advances past the last delivered message. But that boundary was
never returned to the callers. The three callers
(``wait_for_events`` fast path, ``wait_for_events`` slow path, and
``fetch_events_since``) each merge two ADDITIONAL, UN-clamped streams —
``unassigned_task_appeared`` (an unbounded query) and the in-memory
synthetic-event queue — and recompute the cursor as a GLOBAL ``max()``
over everything merged.

The bug: a single ``unassigned_task_appeared`` event with a timestamp
NEWER than the 500th message drags that global ``max()`` PAST
``msg_cap_ts``. The persisted cursor then leaps over messages 501+, so
on the next poll those messages are permanently skipped — re-opening
the exact silent message-loss / control-message-censorship class
BL-R20-1 was supposed to close, via a sibling event source.

The fix: ``_collect_events_for`` propagates ``msg_cap_ts`` to the
callers, and each caller CAPS the merged batch (and therefore the
returned + persisted cursor) to that boundary. Messages 501+ AND the
newer task event are re-collected on the next poll, in order, nothing
skipped.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from tests.harness import with_bearer

pytestmark = pytest.mark.asyncio


_BASE = _dt.datetime(2026, 1, 1, 0, 0, 0)


def _ts(i: int) -> str:
    """A distinct, lexicographically-sortable ISO timestamp for row i."""
    return (_BASE + _dt.timedelta(seconds=i)).isoformat()


def _seed_messages(
    recipient: str,
    count: int,
    *,
    sender: str = "admin",
    start: int = 0,
    content_prefix: str = "msg",
) -> list[str]:
    """Bulk-insert ``count`` messages to ``recipient`` with strictly
    increasing timestamps. Returns the message_ids in timestamp order."""
    from agent_mcp.repositories.message_repository import (
        bulk_insert_messages,
    )

    rows = []
    ids = []
    for k in range(count):
        i = start + k
        mid = f"{content_prefix}-{i:05d}"
        ids.append(mid)
        rows.append({
            "message_id": mid,
            "sender_id": sender,
            "recipient_id": recipient,
            "message_content": f"{content_prefix} body {i}",
            "message_type": "info",
            "priority": "normal",
            "timestamp": _ts(i),
            "delivered": False,
            "read": False,
        })
    written = bulk_insert_messages(rows)
    assert written == count, f"seeded {written}, wanted {count}"
    return ids


def _seed_unassigned_task(
    task_id: str,
    *,
    updated_at: str,
    required_capabilities: str = "[]",
) -> None:
    """Insert one unassigned task whose ``updated_at`` is ``updated_at``.

    ``_collect_unassigned_task_events_for`` surfaces it to any agent
    whose capabilities are a superset of ``required_capabilities`` —
    with the empty default it matches every agent."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes, "
            "required_capabilities) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "Newer than msg 500",
                "",
                None,
                "admin",
                "unassigned",
                "medium",
                updated_at,
                updated_at,
                None,
                "[]",
                "[]",
                "[]",
                required_capabilities,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _message_ids(events: list[dict]) -> list[str]:
    return [
        e["data"]["message_id"]
        for e in events
        if e.get("type") in ("message", "broadcast")
    ]


def _task_ids(events: list[dict]) -> list[str]:
    return [
        e["ref_id"]
        for e in events
        if e.get("type") == "unassigned_task_appeared"
    ]


async def _fetch(agent_token: str, cursor: str) -> dict:
    """Drive the ``fetch_events_since`` caller (pure-DB, non-blocking)
    and return the parsed ``{events, cursor}`` body."""
    import json

    from agent_mcp.tools.agent_communication_tools import (
        fetch_events_since_tool_impl,
    )

    with with_bearer(agent_token):
        result = await fetch_events_since_tool_impl(
            {"token": agent_token, "cursor": cursor}
        )
    # ``Ok.message`` carries the JSON-encoded body (the wire shape).
    return json.loads(result.message)


# ---------------------------------------------------------------------------
# Core regression: a newer unassigned-task event must NOT drag the cursor
# past the message truncation boundary.
# ---------------------------------------------------------------------------


async def test_newer_task_event_does_not_skip_truncated_messages(
    tmp_path: Path,
) -> None:
    """Seed 600 messages + one ``unassigned_task_appeared`` NEWER than
    the 500th message, then drive ``fetch_events_since`` twice.

    RED on main: poll 1 merges the oldest 500 messages with the newer
    task event and advances the persisted cursor to the TASK's
    timestamp — past message 500 — so poll 2 (querying strictly after
    that cursor) returns NOTHING and messages 501..600 are lost forever.

    GREEN after: poll 1's cursor is CAPPED to the 500th message's
    timestamp (the task event is clamped out of this batch), so poll 2
    returns the remaining 100 messages in order followed by the task
    event — nothing skipped.
    """
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        expected_msgs = _seed_messages("alice", 600)
        # A task that transitions to unassigned AFTER msg 500 (at
        # _ts(499)) — in fact after every message — so its event would
        # drag a global max() cursor past the truncation boundary.
        _seed_unassigned_task("task-newer", updated_at=_ts(1000))

        since = _ts(-1)

        # ---- Poll 1 -----------------------------------------------------
        body1 = await _fetch(alice.token, since)
        msg_ids1 = _message_ids(body1["events"])
        assert len(msg_ids1) == 500, (
            f"poll 1 must return the OLDEST 500 messages (the truncated "
            f"prefix), got {len(msg_ids1)}"
        )
        assert msg_ids1 == expected_msgs[:500], (
            "poll 1 must be the oldest 500 messages in timestamp order"
        )
        # The clamp boundary is the 500th message's timestamp; the newer
        # task event must be held back this poll.
        assert _task_ids(body1["events"]) == [], (
            "the newer unassigned-task event must be clamped OUT of the "
            "truncated first batch so it can't drag the cursor forward"
        )
        assert body1["cursor"] == _ts(499), (
            "the persisted cursor must be CAPPED to the 500th message's "
            f"timestamp, not the newer task event; got {body1['cursor']}"
        )

        # ---- Poll 2 -----------------------------------------------------
        body2 = await _fetch(alice.token, body1["cursor"])
        msg_ids2 = _message_ids(body2["events"])
        assert msg_ids2 == expected_msgs[500:], (
            "poll 2 must return the remaining 100 messages in timestamp "
            f"order, none skipped; got {len(msg_ids2)}"
        )
        # Now that the message backlog no longer fills the cap, the
        # deferred task event finally surfaces.
        assert "task-newer" in _task_ids(body2["events"]), (
            "the deferred unassigned-task event must surface once the "
            "message backlog drops below the cap"
        )

        # No message skipped or duplicated across the two polls.
        assert msg_ids1 + msg_ids2 == expected_msgs, (
            "the full 600-message backlog must drain across the two polls "
            "with nothing skipped or duplicated"
        )


# ---------------------------------------------------------------------------
# Regression: the normal (no-truncation) case is unchanged — all streams
# deliver promptly and the cursor sits at the true global max.
# ---------------------------------------------------------------------------


async def test_small_backlog_delivers_all_streams_at_true_max(
    tmp_path: Path,
) -> None:
    """A small message backlog + a newer unassigned-task event fit under
    the cap, so BOTH stream in a single poll and the cursor advances to
    the true global max (the task event's timestamp). The cap only
    engages on truncation."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        expected_msgs = _seed_messages("bob", 3)
        _seed_unassigned_task("task-small", updated_at=_ts(500))

        body = await _fetch(bob.token, _ts(-1))
        assert _message_ids(body["events"]) == expected_msgs, (
            "a small backlog must deliver all messages in one poll"
        )
        assert "task-small" in _task_ids(body["events"]), (
            "the unassigned-task event must deliver in the same poll when "
            "no truncation occurs"
        )
        assert body["cursor"] == _ts(500), (
            "with no truncation the cursor advances to the true global "
            f"max (the task event); got {body['cursor']}"
        )
