"""BL-R30-1 — task reassignment must reconcile ``agents.current_task``.

The terminal-status path clears ``agents.current_task`` when the task it
points at reaches completed/cancelled/failed
(``test_agent_current_task_cleared_on_completion``). But a plain REBIND —
moving a task from agent X to agent Y with no status change — reconciled
NEITHER pointer:

  * the LOSING agent X kept a stale ``current_task`` pointing at a task it
    no longer owns (the exact leak the terminal-clear guard was added
    for), and
  * the GAINING agent Y's ``current_task`` was never set, so Y rendered
    idle in ``/api/all-data`` and the dashboard despite owning the task.

Three reassign surfaces share the gap, all fixed here via a shared
``AgentRepository.reconcile_current_task_on_reassign`` helper:

  1. MCP single reassign  — ``_update_single_task`` (update_task_status
     with ``assigned_to``)
  2. MCP bulk reassign    — ``bulk_task_operations`` ``reassign`` op
  3. REST dashboard       — ``POST /api/update-task-dashboard`` assigned_to

Plus the REST create-with-assignee sibling
(``POST /api/tasks`` with ``assigned_to``) which never set the gaining
agent's ``current_task``, unlike the MCP create+assign paths.

All assertions read ``agents.current_task`` straight from SQLite — the
authoritative source, not a session-scoped cache.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_agent(agent_id: str, *, current_task: str | None = None) -> None:
    """Insert a live agent row (+ cache mirror), optionally pre-pinned to
    ``current_task``."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    token = secrets.token_hex(16)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at, agent_role, "
        "current_task) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            token, agent_id, json.dumps([]), now, "active",
            "/tmp", "#888", now, "worker", current_task,
        ),
    )
    conn.commit()
    conn.close()
    g.active_agents[token] = {
        "agent_id": agent_id,
        "status": "active",
        "created_at": now,
        "capabilities": [],
        "agent_role": "worker",
        "current_task": current_task,
    }


def _seed_task(
    title: str, assigned_to: str | None, *, status: str = "pending",
) -> str:
    """Insert a task row (+ cache)."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, title, "seed", status, "low",
            assigned_to, "admin", now, now, "[]",
        ),
    )
    conn.commit()
    conn.close()
    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "status": status,
        "priority": "low",
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "notes": [],
    }
    return task_id


def _current_task(agent_id: str) -> str | None:
    """Read ``agents.current_task`` straight from SQLite (bypasses caches)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT current_task FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["current_task"] if row else None


# ==========================================================================
# 1. MCP single reassign (_update_single_task via update_task_status)
# ==========================================================================


async def test_single_reassign_reconciles_current_task(tmp_path) -> None:
    """update_task_status reassign X->Y: X (who pointed at T) is cleared,
    Y (idle) gains T. RED on origin/main (neither pointer moves)."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_task("do thing", "alice")
        _seed_agent("alice", current_task=task_id)
        _seed_agent("bob", current_task=None)

        result = await admin.call(
            "update_task_status",
            {"task_id": task_id, "status": "in_progress",
             "assigned_to": "bob"},
        )
        text = result[0].text
        assert "Error" not in text, text

        assert _current_task("alice") is None, (
            "losing agent alice still points at reassigned task"
        )
        assert _current_task("bob") == task_id, (
            "gaining agent bob's current_task was never set"
        )


async def test_single_reassign_does_not_clobber_busy_gainer(tmp_path) -> None:
    """If the gaining agent already holds a DIFFERENT current_task, the
    reassign must not overwrite it (mirror the assign path: set only when
    NULL)."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_task("do thing", "alice")
        other = _seed_task("bob's own", "bob", status="in_progress")
        _seed_agent("alice", current_task=task_id)
        _seed_agent("bob", current_task=other)

        result = await admin.call(
            "update_task_status",
            {"task_id": task_id, "status": "in_progress",
             "assigned_to": "bob"},
        )
        assert "Error" not in result[0].text, result[0].text

        assert _current_task("alice") is None
        assert _current_task("bob") == other, (
            "reassign clobbered bob's existing current_task"
        )


async def test_single_reassign_leaves_unrelated_loser_pointer(tmp_path) -> None:
    """If the losing agent's current_task points at a DIFFERENT task than
    the one being reassigned, it must be left alone (scoped clear)."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_task("do thing", "alice")
        alice_other = _seed_task("alice's other", "alice", status="in_progress")
        _seed_agent("alice", current_task=alice_other)
        _seed_agent("bob", current_task=None)

        await admin.call(
            "update_task_status",
            {"task_id": task_id, "status": "in_progress",
             "assigned_to": "bob"},
        )

        assert _current_task("alice") == alice_other, (
            "reassign wrongly cleared alice's unrelated current_task"
        )
        assert _current_task("bob") == task_id


# ==========================================================================
# 2. MCP bulk reassign (bulk_task_operations reassign op)
# ==========================================================================


async def test_bulk_reassign_reconciles_current_task(tmp_path) -> None:
    """Bulk reassign X->Y reconciles both pointers. RED on origin/main."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_task("do thing", "alice")
        _seed_agent("alice", current_task=task_id)
        _seed_agent("bob", current_task=None)

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id, "assigned_to": "bob"},
            ]},
        )
        assert "reassigned to 'bob'" in result[0].text, result[0].text

        assert _current_task("alice") is None
        assert _current_task("bob") == task_id


# ==========================================================================
# 3. REST dashboard reassign (POST /api/update-task-dashboard)
# ==========================================================================


async def test_dashboard_reassign_reconciles_current_task(tmp_path) -> None:
    """Dashboard PATCH reassign X->Y reconciles both. RED on origin/main."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_task("do thing", "alice")
        _seed_agent("alice", current_task=task_id)
        _seed_agent("bob", current_task=None)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={"token": admin.admin_token, "task_id": task_id,
                  "assigned_to": "bob"},
        )
        assert r.status_code == 200, r.text

        assert _current_task("alice") is None
        assert _current_task("bob") == task_id


async def test_dashboard_clear_assignment_clears_only_loser(tmp_path) -> None:
    """Clear-assignment (assigned_to -> none): the losing agent is cleared,
    and there is no gainer to set."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_task("do thing", "alice")
        _seed_agent("alice", current_task=task_id)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={"token": admin.admin_token, "task_id": task_id,
                  "assigned_to": "unassigned"},
        )
        assert r.status_code == 200, r.text

        assert _current_task("alice") is None, (
            "clear-assignment left the losing agent pinned to the task"
        )


# ==========================================================================
# 4. REST create-with-assignee sibling (POST /api/tasks assigned_to)
# ==========================================================================


async def test_rest_create_with_assignee_sets_current_task(tmp_path) -> None:
    """A REST-created task assigned to an idle agent must set that agent's
    current_task, mirroring the MCP create+assign paths. RED on origin/main
    (REST create never wired current_task)."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("bob", current_task=None)

        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "fresh task",
                "task_description": "...",
                "assigned_to": "bob",
            },
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]

        assert _current_task("bob") == task_id, (
            "REST create-with-assignee left the gaining agent idle"
        )


async def test_rest_create_with_busy_assignee_preserves_current_task(
    tmp_path,
) -> None:
    """Create-with-assignee must not clobber an agent that already holds a
    current_task (set only when NULL)."""
    async with mcp_session(tmp_path) as admin:
        busy = _seed_task("bob busy", "bob", status="in_progress")
        _seed_agent("bob", current_task=busy)

        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "another",
                "task_description": "...",
                "assigned_to": "bob",
            },
        )
        assert r.status_code == 200, r.text

        assert _current_task("bob") == busy, (
            "create-with-assignee clobbered bob's existing current_task"
        )
