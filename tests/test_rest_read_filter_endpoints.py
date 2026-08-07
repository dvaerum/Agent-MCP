"""Read REST endpoints with filters (Phase 7c).

Three new query-param-based filters extend the existing read endpoints
so they can replace the router's read-only synthetic tools
(`list_agents`, `list_tasks_for`, `list_unassigned_tasks`):

- GET /api/agents?status=<status>   (no param = all rows, existing behavior)
- GET /api/tasks?assigned_to=<id>   (no param = all rows, existing behavior)
- GET /api/tasks?unassigned=true    (assigned_to IS NULL)

All three are dashboard-as-admin reads: unauthenticated callers see
everything (matches `/api/tokens` and the existing list endpoints).
Filtering is exact-match (no LIKE/wildcards). Empty result sets are
returned as `[]`, not 404.

Backward compat: calling the endpoints with no query params returns the
same shape as before this PR.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_agent(agent_id: str, status: str = "active") -> str:
    """Insert an agent row directly, return its token.

    Matches the helper in `test_assign_task_agent_token.py`, but takes
    a status so we can build a fixture set with mixed statuses.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()
    terminated_at = now if status == "terminated" else None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, created_at, "
        "status, working_directory, color, terminated_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, agent_id, now, status, "/tmp", "#888",
         terminated_at, now),
    )
    conn.commit()
    conn.close()

    if status != "terminated":
        g.active_agents[token] = {
            "agent_id": agent_id,
            "status": status,
            "created_at": now,
        }
    return token


def _seed_task(task_id: str, *, assigned_to: str | None,
               status: str = "pending") -> None:
    """Insert a task row directly with a given assignment + status."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, assigned_to, "
        "created_by, status, priority, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, task_id, "seeded by test", assigned_to, "admin",
         status, "medium", now, now),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# GET /api/agents?status=<status>
# ---------------------------------------------------------------------------


async def test_agents_filter_status_active_excludes_terminated(tmp_path) -> None:
    """`?status=active` returns only active agents (no terminated rows)."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("alice", status="active")
        _seed_agent("bob", status="active")
        _seed_agent("zombie", status="terminated")

        r = admin.client.get("/api/agents?status=active")
        assert r.status_code == 200, r.text
        rows = r.json()
        # Wave 4 retired the synthetic 'Admin' row this endpoint used
        # to prepend; the status filter no longer needs to drop it. Only
        # real agent rows with status='active' should survive.
        ids = [row.get("agent_id") for row in rows]
        statuses = [row.get("status") for row in rows]
        assert "alice" in ids, ids
        assert "bob" in ids, ids
        assert "zombie" not in ids, ids
        assert set(statuses) == {"active"}, statuses


async def test_agents_filter_status_terminated_only(tmp_path) -> None:
    """`?status=terminated` returns only terminated agents."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("alice", status="active")
        _seed_agent("zombie", status="terminated")
        _seed_agent("ghost", status="terminated")

        r = admin.client.get("/api/agents?status=terminated")
        assert r.status_code == 200, r.text
        rows = r.json()
        ids = [row.get("agent_id") for row in rows]
        assert ids and set(ids) == {"zombie", "ghost"}, ids
        assert all(row.get("status") == "terminated" for row in rows)


async def test_agents_no_filter_returns_all(tmp_path) -> None:
    """No query params → every persisted (non-tombstone) agent row.

    Wave 4 (cleanup/wave-4-delete-admin-pseudo-agent) retired the
    synthetic 'Admin' row this endpoint used to prepend; it must not
    surface here any more."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("alice", status="active")
        _seed_agent("zombie", status="terminated")

        r = admin.client.get("/api/agents")
        assert r.status_code == 200, r.text
        rows = r.json()
        ids = [row.get("agent_id") for row in rows]
        assert "alice" in ids, ids
        assert "zombie" in ids, ids
        # Wave 4: the synthetic 'Admin' row is gone.
        assert "Admin" not in ids, (
            f"synthetic 'Admin' row resurfaced post-Wave-4: {ids}"
        )


# ---------------------------------------------------------------------------
# GET /api/tasks?assigned_to=<agent_id>
# ---------------------------------------------------------------------------


async def test_tasks_filter_assigned_to_existing_agent(tmp_path) -> None:
    """`?assigned_to=alice` returns only tasks whose assigned_to=='alice'."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("alice", status="active")
        _seed_agent("bob", status="active")
        _seed_task("task_a1", assigned_to="alice")
        _seed_task("task_a2", assigned_to="alice")
        _seed_task("task_b1", assigned_to="bob")
        _seed_task("task_u1", assigned_to=None)

        r = admin.client.get("/api/tasks?assigned_to=alice")
        assert r.status_code == 200, r.text
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("tasks", [])
        ids = sorted(row.get("task_id") for row in rows)
        assert ids == ["task_a1", "task_a2"], ids
        assert all(row.get("assigned_to") == "alice" for row in rows)


async def test_tasks_filter_assigned_to_nonexistent_agent_is_empty(
    tmp_path,
) -> None:
    """`?assigned_to=ghost` (no such agent) returns []; not 404."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("alice", status="active")
        _seed_task("task_a1", assigned_to="alice")

        r = admin.client.get("/api/tasks?assigned_to=ghost-does-not-exist")
        assert r.status_code == 200, r.text
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("tasks", [])
        assert rows == [], rows


# ---------------------------------------------------------------------------
# GET /api/tasks?unassigned=true
# ---------------------------------------------------------------------------


async def test_tasks_filter_unassigned(tmp_path) -> None:
    """`?unassigned=true` returns only tasks with assigned_to IS NULL."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("alice", status="active")
        _seed_task("task_assigned", assigned_to="alice")
        _seed_task("task_unassigned_1", assigned_to=None)
        _seed_task("task_unassigned_2", assigned_to=None)

        r = admin.client.get("/api/tasks?unassigned=true")
        assert r.status_code == 200, r.text
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("tasks", [])
        ids = sorted(row.get("task_id") for row in rows)
        assert ids == ["task_unassigned_1", "task_unassigned_2"], ids
        assert all(row.get("assigned_to") is None for row in rows)


async def test_tasks_no_filter_returns_all(tmp_path) -> None:
    """No query params → backward-compatible (every task row, no filter)."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("alice", status="active")
        _seed_task("task_assigned", assigned_to="alice")
        _seed_task("task_unassigned", assigned_to=None)

        r = admin.client.get("/api/tasks")
        assert r.status_code == 200, r.text
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("tasks", [])
        ids = sorted(row.get("task_id") for row in rows)
        assert ids == ["task_assigned", "task_unassigned"], ids
