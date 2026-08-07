"""When a task transitions to a terminal status (completed / cancelled /
failed), the assigned agent's ``current_task`` column MUST be cleared.

Live-production evidence (washing-brothers project, 2026-06-04):

  agent_id      | current_task         | tasks.status
  ------------- | -------------------- | ------------
  ios-app-dev   | task_9c2c1f9227aa    | completed

The agent's `current_task` keeps pointing at a long-finished task,
so every downstream surface (REST /api/all-data, MCP tool responses,
dashboard "current task" indicator) reports that the agent is still
"on" a task that's done.

Root cause: `agent_mcp/tools/task_tools.py::_update_single_task`
updates `tasks.status` but never reaches into the `agents` table to
NULL-out `current_task`. Same for the in-memory
`g.active_agents[token]["current_task"]` mirror.

Contract this test pins in place:
  When `update_task_status` flips a task to completed / cancelled /
  failed, every agent whose `current_task == task_id` MUST be reset
  to NULL — at the DB level AND in the in-memory cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_mcp.core import globals as g
from agent_mcp.db.connection import get_db_connection
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _current_task_for(agent_id: str) -> str | None:
    """Read `agents.current_task` straight from SQLite (bypasses caches)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT current_task FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cur.fetchone()
        return row["current_task"] if row else None
    finally:
        conn.close()


def _force_current_task(agent_id: str, task_id: str) -> None:
    """Set `agents.current_task` directly — simulates the state left
    behind by the MCP `assign_task` tool (the REST `/api/tasks` POST
    doesn't wire `current_task`, only the MCP tool does)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agents SET current_task = ? WHERE agent_id = ?",
            (task_id, agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    # Mirror into the in-memory active-agents cache used by some
    # code paths.
    for entry in g.active_agents.values():
        if entry.get("agent_id") == agent_id:
            entry["current_task"] = task_id
            break


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled", "failed"])
async def test_agent_current_task_cleared_when_task_reaches_terminal_status(
    tmp_path: Path, terminal_status: str
) -> None:
    """`agents.current_task` must be NULL after the task it points at
    transitions to a terminal status."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        # Admin creates + assigns a task to alice. The assign_task
        # codepath in task_tools.py already sets
        # `agents.current_task = task_id` when the agent has no
        # current task.
        r = admin.post(
            "/api/tasks",
            json={
                "task_title": "do thing",
                "task_description": "...",
                "assigned_to": alice.agent_id,
            },
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]

        # The MCP `assign_task` tool sets `agents.current_task` when
        # the agent has no active task; the REST `/api/tasks` POST
        # used above does not. Force-set it here so we're testing the
        # CLEAR-on-terminal behaviour, not the SET-on-assign one.
        _force_current_task(alice.agent_id, task_id)
        assert _current_task_for(alice.agent_id) == task_id

        # Worker (or admin) marks the task terminal.
        await alice.call(
            "update_task_status",
            {"task_id": task_id, "status": terminal_status},
        )

        # DB-level: the agent row must no longer point at the
        # finished task.
        assert _current_task_for(alice.agent_id) is None, (
            f"agents.current_task still points at task {task_id} after "
            f"status={terminal_status} — leaks stale assignment to "
            f"every consumer of /api/all-data"
        )

        # In-memory cache: must mirror the DB.
        active = g.active_agents.get(alice.agent_id, {})
        assert active.get("current_task") is None, (
            f"g.active_agents[{alice.agent_id!r}].current_task still "
            f"points at finished task — in-memory mirror diverged from DB"
        )


async def test_other_agents_current_task_untouched_when_unrelated_task_completes(
    tmp_path: Path,
) -> None:
    """The clear-on-terminal logic must scope to the actual assignee:
    completing alice's task must NOT touch bob's current_task."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        # One task each.
        r_a = admin.post(
            "/api/tasks",
            json={
                "task_title": "alice task",
                "task_description": "...",
                "assigned_to": alice.agent_id,
            },
        )
        task_a = r_a.json()["task_id"]

        r_b = admin.post(
            "/api/tasks",
            json={
                "task_title": "bob task",
                "task_description": "...",
                "assigned_to": bob.agent_id,
            },
        )
        task_b = r_b.json()["task_id"]

        _force_current_task(alice.agent_id, task_a)
        _force_current_task(bob.agent_id, task_b)
        assert _current_task_for(alice.agent_id) == task_a
        assert _current_task_for(bob.agent_id) == task_b

        # Complete alice's task.
        await alice.call(
            "update_task_status",
            {"task_id": task_a, "status": "completed"},
        )

        # alice cleared, bob unchanged.
        assert _current_task_for(alice.agent_id) is None
        assert _current_task_for(bob.agent_id) == task_b, (
            "completing alice's task wrongly cleared bob's current_task — "
            "the clear must be scoped to `WHERE current_task = ?`"
        )
