"""arch-deepening round-2 #2 — one ``assemble_event_feed`` owner + the
two fold-in fixes.

Two invariants pinned here (both RED on main):

1. **inbox ≡ wait_for_events**: the inbox resource and
   ``wait_for_events`` must return the IDENTICAL event list for the same
   cursor. On main ``resources/inbox`` calls the narrower
   ``_collect_events_for`` helper, which OMITS the unassigned-task
   stream (and the merged-boundary clamp) that ``wait_for_events``
   merges in — so an ``unassigned_task_appeared`` event surfaces in one
   surface but not the other, despite inbox's docstring claiming
   byte-identical output. Routing BOTH through ``assemble_event_feed``
   makes the divergence unrepresentable.

2. **no self-wake on a no-op cursor advance**: ``advance_event_cursor``
   published ``agent.updated`` UNCONDITIONALLY on every cursor write.
   ``agent.updated`` fans out to ``state.notify_waiters`` → every
   sibling ``wait_for_events`` waiter for that agent wakes and re-queries
   for nothing. Under fan-out (N concurrent waiters each writing the same
   high-water cursor) that is O(N) spurious wakes per event round. A
   no-op advance (cursor <= current) must fire NO wake at all.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


_BASE = _dt.datetime(2026, 1, 1, 0, 0, 0)


def _ts(i: int) -> str:
    return (_BASE + _dt.timedelta(seconds=i)).isoformat()


def _seed_unassigned_task(
    task_id: str,
    *,
    updated_at: str,
    required_capabilities: str = "[]",
) -> None:
    """Insert one unassigned task (empty required caps → matches every
    agent) whose ``updated_at`` is ``updated_at``."""
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
                "unassigned work",
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


# ---------------------------------------------------------------------------
# Invariant 1: inbox and wait_for_events return the identical event list.
# ---------------------------------------------------------------------------


async def test_inbox_and_wait_for_events_identical_for_same_cursor(
    tmp_path: Path,
) -> None:
    """A message AND a matching unassigned task exist since the cursor.
    ``wait_for_events`` (fast path) surfaces both; the inbox resource
    must surface the SAME two events. RED on main (inbox omits the
    unassigned-task stream)."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
        wait_for_events_tool_impl,
    )
    from agent_mcp.resources.inbox import render_inbox

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        await send_agent_message_tool_impl(
            {
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message": "hello alice",
                "deliver_method": "store",
            }
        )
        _seed_unassigned_task("task-x", updated_at=_ts(500))

        since = _ts(-1)

        wait_result = await wait_for_events_tool_impl(
            {"token": alice.token, "since": since, "timeout_seconds": 1}
        )
        wait_events = wait_result.data["events"]

        inbox_payload = json.loads(render_inbox("alice", since))
        inbox_events = inbox_payload["events"]

        # Both surfaces must agree on the unassigned-task event...
        wait_types = sorted(e["type"] for e in wait_events)
        inbox_types = sorted(e["type"] for e in inbox_events)
        assert "unassigned_task_appeared" in wait_types, (
            "wait_for_events must surface the matching unassigned task"
        )
        # ...and the two event lists must be byte-identical.
        assert inbox_events == wait_events, (
            "inbox and wait_for_events diverge for the same cursor:\n"
            f"  inbox={inbox_types}\n  wait ={wait_types}"
        )
        assert inbox_payload["next_cursor"] == wait_result.data["next_cursor"]


# ---------------------------------------------------------------------------
# Invariant 2: a no-op cursor advance fires no agent-wake.
# ---------------------------------------------------------------------------


async def test_noop_cursor_advance_does_not_publish_agent_updated(
    tmp_path: Path,
) -> None:
    """A cursor advance that does NOT change ``last_event_seen_at``
    (equal or lower value) must not publish ``agent.updated`` — that
    event wakes every sibling ``wait_for_events`` waiter for nothing.
    RED on main (publishes unconditionally)."""
    from tests.harness import mcp_session
    from agent_mcp.repositories import agent_repo
    import agent_mcp.repositories.agent_repository as agent_repository

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        published: list[tuple[str, str]] = []
        orig_publish = agent_repository._publish

        def _spy(addressee, event, payload):  # noqa: ANN001
            published.append((event, (payload or {}).get("field")))
            return orig_publish(addressee, event, payload)

        agent_repository._publish = _spy
        try:
            # First advance: NULL -> _ts(10) is a REAL change; it may
            # publish (that's fine — a genuine state change).
            assert agent_repo.advance_event_cursor("alice", _ts(10)) is True
            published.clear()

            # No-op advances: equal, then lower. MAX semantics keep the
            # column at _ts(10), so nothing changed → nothing published.
            assert agent_repo.advance_event_cursor("alice", _ts(10)) is True
            assert agent_repo.advance_event_cursor("alice", _ts(5)) is True
        finally:
            agent_repository._publish = orig_publish

        cursor_updates = [
            (evt, field)
            for (evt, field) in published
            if evt == "agent.updated" and field == "last_event_seen_at"
        ]
        assert cursor_updates == [], (
            "a no-op cursor advance must NOT publish agent.updated "
            f"(would self-wake every sibling waiter); got {cursor_updates}"
        )


async def test_real_cursor_advance_still_persists_monotonically(
    tmp_path: Path,
) -> None:
    """The self-wake fix must not weaken the MAX-based monotonic advance:
    a higher cursor moves the watermark, a lower one never rewinds it."""
    from tests.harness import mcp_session
    from agent_mcp.repositories import agent_repo
    from agent_mcp.tools.agent_communication_tools import (
        _read_last_event_seen_at,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        assert agent_repo.advance_event_cursor("alice", _ts(10)) is True
        assert _read_last_event_seen_at("alice") == _ts(10)

        # Lower value must not rewind.
        assert agent_repo.advance_event_cursor("alice", _ts(3)) is True
        assert _read_last_event_seen_at("alice") == _ts(10)

        # Higher value advances.
        assert agent_repo.advance_event_cursor("alice", _ts(20)) is True
        assert _read_last_event_seen_at("alice") == _ts(20)
