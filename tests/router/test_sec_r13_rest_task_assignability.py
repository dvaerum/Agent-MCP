"""BL-R13-1: REST task-assignment must honour the assignability invariant.

The canonical MCP task paths gate every ``assigned_to`` write on
``_agent_assignable(cursor, agent_id)`` (task_tools.py) — True only if
the agent exists AND is not terminated. Two REST handlers wrote
``assigned_to`` directly, bypassing it:

  * ``POST /api/tasks`` (create) — ``tasks.py``;
  * ``POST /api/update-task-dashboard`` (reassign) — ``composition.py``.

Either could persist a task pinned on a nonexistent or terminated agent
behind an HTTP 200 — unreachable work attributed to a dead identity.
This is the ``assigned_to`` sibling of BL-R12-1's ``status`` fix.

RED on origin/main (bad pin stored, 200); GREEN after both REST paths
enforce ``_agent_assignable``.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _row(table: str, where_sql: str, params: tuple) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
        r = cursor.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _terminate(agent_id: str) -> None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE agents SET status = 'terminated' WHERE agent_id = ?",
            (agent_id,),
        )
        conn.commit()
    finally:
        conn.close()


async def _create_task(admin, **body_extra):
    body = {"task_title": "assign-probe"}
    body.update(body_extra)
    return admin.post("/api/tasks", json=body)


# ===================== create path (tasks.py) ===================== #


async def test_create_task_to_nonexistent_agent_rejected(tmp_path) -> None:
    """Creating a task assigned to an agent that does not exist must be
    rejected (4xx), not persisted behind a 200."""
    async with mcp_session(tmp_path) as admin:
        r = await _create_task(admin, assigned_to="ghost-does-not-exist")
        assert r.status_code >= 400, (
            f"assigning a new task to a nonexistent agent must be rejected, "
            f"got {r.status_code}: {r.text}"
        )
        # No task row may have been pinned on the ghost.
        assert _row("tasks", "assigned_to = ?", ("ghost-does-not-exist",)) is None


async def test_create_task_to_terminated_agent_rejected(tmp_path) -> None:
    """Creating a task assigned to a terminated agent must be rejected."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("zombie")
        _terminate("zombie")

        r = await _create_task(admin, assigned_to="zombie")
        assert r.status_code >= 400, (
            f"assigning a new task to a terminated agent must be rejected, "
            f"got {r.status_code}: {r.text}"
        )
        assert _row("tasks", "assigned_to = ?", ("zombie",)) is None


async def test_create_task_to_live_agent_succeeds(tmp_path) -> None:
    """Regression: assigning a new task to a live agent still succeeds."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = await _create_task(admin, assigned_to="alice")
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]
        assert _row("tasks", "task_id = ?", (task_id,))["assigned_to"] == "alice"


async def test_create_unassigned_task_still_allowed(tmp_path) -> None:
    """Regression: an unassigned task (no assigned_to) is still allowed."""
    async with mcp_session(tmp_path) as admin:
        r = await _create_task(admin)
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]
        assert _row("tasks", "task_id = ?", (task_id,))["assigned_to"] in (None, "")


# ============== reassign path (composition.py dashboard) ============== #


async def test_dashboard_reassign_to_nonexistent_agent_rejected(tmp_path) -> None:
    """Reassigning a task to a nonexistent agent via the dashboard must be
    rejected, and must not overwrite the existing assignment."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = await _create_task(admin, assigned_to="alice")
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]

        r2 = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "assigned_to": "ghost-does-not-exist",
            },
        )
        assert r2.status_code >= 400, (
            f"dashboard reassignment to a nonexistent agent must be rejected, "
            f"got {r2.status_code}: {r2.text}"
        )
        # DB is authoritative: still assigned to alice, never re-pinned.
        assert _row("tasks", "task_id = ?", (task_id,))["assigned_to"] == "alice"


async def test_dashboard_reassign_to_terminated_agent_rejected(tmp_path) -> None:
    """Reassigning a task to a terminated agent via the dashboard must be
    rejected."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("zombie")
        _terminate("zombie")
        r = await _create_task(admin, assigned_to="alice")
        task_id = r.json()["task_id"]

        r2 = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "assigned_to": "zombie",
            },
        )
        assert r2.status_code >= 400, (
            f"dashboard reassignment to a terminated agent must be rejected, "
            f"got {r2.status_code}: {r2.text}"
        )
        assert _row("tasks", "task_id = ?", (task_id,))["assigned_to"] == "alice"


async def test_dashboard_reassign_to_live_agent_succeeds(tmp_path) -> None:
    """Regression: reassignment to a live agent still lands."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        r = await _create_task(admin, assigned_to="alice")
        task_id = r.json()["task_id"]

        r2 = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "assigned_to": "bob",
            },
        )
        assert r2.status_code == 200, r2.text
        assert _row("tasks", "task_id = ?", (task_id,))["assigned_to"] == "bob"


async def test_dashboard_unassign_still_allowed(tmp_path) -> None:
    """Regression: clearing the assignment (unassigned) via the dashboard
    is still allowed — an empty assignment is not a bad pin."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = await _create_task(admin, assigned_to="alice")
        task_id = r.json()["task_id"]

        r2 = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "assigned_to": "unassigned",
            },
        )
        assert r2.status_code == 200, r2.text
        assert _row("tasks", "task_id = ?", (task_id,))["assigned_to"] in (None, "")
