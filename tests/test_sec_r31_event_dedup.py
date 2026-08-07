"""BL-R31-2: ``wait_for_events`` must not deliver the same
``unassigned_task_appeared`` event twice per envelope.

The event-collection merge path in ``wait_for_events_tool_impl`` /
``fetch_events_since_tool_impl`` combines TWO sources of
``unassigned_task_appeared`` events for the same logical task:

  1. A DB re-query copy — rows re-queried by
     :func:`_collect_unassigned_task_events_for`, timestamped by the
     task's ``updated_at`` (the transition-to-unassigned time).
  2. A synthetic in-memory queue copy — pushed by
     ``notify_unassigned_task_appeared`` through the EventBus onto the
     waiter's private queue, timestamped by wall-clock ``now()``.

Pre-fix the merge/sort/cap path had NO dedup, so the SAME unassigned
task appeared TWICE in one envelope (with two different timestamps).
An auto-claiming worker that reacts to each event double-claims the
same task.

Fix: dedup the merged stream by a stable event identity
(``(type, task_id)`` for ``unassigned_task_appeared``), keeping the
DB ``updated_at``-timestamped copy so the BL-R20-1 oldest-first and
BL-R21-1 clamp/cursor invariants stay correct.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


_DB_UPDATED_AT = "2026-01-01T00:00:00"


def _envelope(blocks) -> dict:
    assert blocks, "tool returned no content blocks"
    return json.loads(blocks[0].text)


def _seed_unassigned_task(
    task_id: str,
    *,
    updated_at: str = _DB_UPDATED_AT,
) -> None:
    """Insert one unassigned task whose ``updated_at`` is ``updated_at``.

    ``_collect_unassigned_task_events_for`` surfaces every unassigned
    task to every agent (capability-tag routing retired in PR5)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                f"Unassigned {task_id}",
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
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _appearances(events: list[dict], task_id: str) -> list[dict]:
    """Every ``unassigned_task_appeared`` event carrying ``task_id``,
    across both the top-level ``ref_id`` and the ``payload.task_id``
    shapes the two sources emit."""
    out = []
    for e in events:
        if e.get("type") != "unassigned_task_appeared":
            continue
        ref = e.get("ref_id")
        payload_id = (e.get("payload") or {}).get("task_id")
        if ref == task_id or payload_id == task_id:
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# Core regression (RED on main): a single unassigned task that lands in
# BOTH the DB re-query stream AND the synthetic queue must appear EXACTLY
# ONCE in the returned envelope.
# ---------------------------------------------------------------------------


async def test_unassigned_task_delivered_exactly_once(tmp_path: Path) -> None:
    """Park a waiter, then make an unassigned task appear via BOTH
    sources at once (the real production sequence: the task row is
    written, then ``notify_unassigned_task_appeared`` fans out a
    synthetic wake-edge copy). The waiter wakes, re-queries the DB
    (DB copy, ``updated_at``-timestamped) AND drains its queue
    (synthetic copy, wall-clock-timestamped).

    RED on main: both copies survive the merge -> the task appears
    TWICE with two different timestamps -> an auto-claimer double-claims.
    GREEN after: dedup collapses them to a single delivery.
    """
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 5}
            )

        task = asyncio.create_task(waiter())
        # Let the waiter enter the slow path (nothing to return yet).
        await asyncio.sleep(0.3)

        # The task transitions to unassigned in the DB (updated_at =
        # the transition time), then the notifier fans out a synthetic
        # wake-edge copy (wall-clock now()) and wakes the parked waiter.
        _seed_unassigned_task("dup-task", updated_at=_DB_UPDATED_AT)
        g.notify_unassigned_task_appeared("dup-task")

        blocks = await task
        body = _envelope(blocks)
        events = body.get("events", [])

        seen = _appearances(events, "dup-task")
        assert len(seen) == 1, (
            "the same unassigned task must be delivered EXACTLY once per "
            f"envelope; got {len(seen)} copies: {seen}"
        )

        # The surviving copy must be the DB ``updated_at``-timestamped
        # one (not the wall-clock synthetic copy) so the BL-R20-1 /
        # BL-R21-1 cursor + clamp semantics stay anchored to the DB
        # transition time.
        assert seen[0].get("timestamp") == _DB_UPDATED_AT, (
            "dedup must keep the DB updated_at-timestamped copy so the "
            f"cursor stays correct; got timestamp {seen[0].get('timestamp')}"
        )
        # And the cursor must not have leapt to the wall-clock now().
        assert body.get("next_cursor") == _DB_UPDATED_AT, (
            "next_cursor must ride the DB updated_at, not the synthetic "
            f"wall-clock copy; got {body.get('next_cursor')}"
        )


# ---------------------------------------------------------------------------
# Regression: genuinely-distinct tasks are NOT collapsed — dedup keys on
# task_id, so two different unassigned tasks each appear once.
# ---------------------------------------------------------------------------


async def test_distinct_unassigned_tasks_not_collapsed(
    tmp_path: Path,
) -> None:
    """Two different unassigned tasks must both be delivered — the
    dedup must key on identity, not collapse the whole event type."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 5}
            )

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.3)

        _seed_unassigned_task("task-a", updated_at="2026-01-01T00:00:00")
        _seed_unassigned_task("task-b", updated_at="2026-01-01T00:00:01")
        g.notify_unassigned_task_appeared("task-a")
        g.notify_unassigned_task_appeared("task-b")

        blocks = await task
        events = _envelope(blocks).get("events", [])

        assert len(_appearances(events, "task-a")) == 1, (
            f"task-a must appear exactly once; events={events}"
        )
        assert len(_appearances(events, "task-b")) == 1, (
            f"task-b must appear exactly once; events={events}"
        )


# ---------------------------------------------------------------------------
# Regression: a single-source unassigned task (DB re-query only, no
# synthetic push) is still delivered once.
# ---------------------------------------------------------------------------


async def test_single_source_unassigned_task_delivered_once(
    tmp_path: Path,
) -> None:
    """When only the DB re-query copy exists (no synthetic queue push),
    the task is still delivered exactly once via the catch-up path."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _seed_unassigned_task("solo-task", updated_at="2026-01-01T00:00:00")

        blocks = await alice.call(
            "fetch_events_since", {"cursor": "2025-01-01T00:00:00"}
        )
        body = json.loads(blocks[0].text)
        events = body.get("events", [])
        assert len(_appearances(events, "solo-task")) == 1, (
            f"single-source task must appear exactly once; events={events}"
        )
