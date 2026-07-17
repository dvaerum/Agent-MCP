"""Task-discovery filters on ``GET /api/tasks`` (REST).

Extends the operator-gated dashboard task listing with the remaining
discovery filters, mirroring the ones the MCP ``view_tasks`` /
``search_tasks`` tools gained in 5.18.0. This endpoint is operator-gated
(the dashboard operator sees ALL tasks), so these filters only NARROW
the operator's full list — there is no worker-visibility gate here.

Params (all optional, AND-combined with each other + the pre-existing
``assigned_to`` / ``unassigned``):

- ``status=<concrete|incomplete|active|open>`` — via the shared
  ``status_filter_matches`` helper (single source with the MCP tools):
  a concrete status is exact-match; the ``incomplete``/``active``/``open``
  pseudo-values expand to any non-terminal status (pending + in_progress).
- ``created_by=<agent_id>`` — exact match on the task's ``created_by``.
- ``assigned=true`` — only tasks that HAVE an assignee (the complement
  of ``unassigned=true``).

Uses the ``tests/harness.py::mcp_session`` harness (same as
``test_rest_read_filter_endpoints.py``); the operator sees every task.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_task(
    task_id: str,
    *,
    assigned_to: str | None = None,
    created_by: str = "admin",
    status: str = "pending",
) -> None:
    """Insert a task row directly with varied assignment/creator/status."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, assigned_to, "
        "created_by, status, priority, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, task_id, "seeded by test", assigned_to, created_by,
         status, "medium", now, now),
    )
    conn.commit()
    conn.close()


def _ids(rows) -> list[str]:
    if isinstance(rows, dict):
        rows = rows.get("tasks", [])
    return sorted(row.get("task_id") for row in rows)


# ---------------------------------------------------------------------------
# status=<pseudo|concrete>
# ---------------------------------------------------------------------------


async def test_status_incomplete_returns_non_terminal(tmp_path) -> None:
    """``?status=incomplete`` → pending + in_progress, excludes terminal."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_pending", status="pending")
        _seed_task("t_inprog", status="in_progress")
        _seed_task("t_done", status="completed")
        _seed_task("t_cancelled", status="cancelled")
        _seed_task("t_failed", status="failed")

        r = admin.client.get("/api/tasks?status=incomplete")
        assert r.status_code == 200, r.text
        assert _ids(r.json()) == ["t_inprog", "t_pending"]


async def test_status_active_and_open_aliases(tmp_path) -> None:
    """``active`` and ``open`` are the same pseudo-value as ``incomplete``."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_pending", status="pending")
        _seed_task("t_done", status="completed")

        for alias in ("active", "open"):
            r = admin.client.get(f"/api/tasks?status={alias}")
            assert r.status_code == 200, r.text
            assert _ids(r.json()) == ["t_pending"], alias


async def test_status_concrete_is_exact_match(tmp_path) -> None:
    """``?status=pending`` is exact — excludes in_progress (no regression)."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_pending", status="pending")
        _seed_task("t_inprog", status="in_progress")
        _seed_task("t_done", status="completed")

        r = admin.client.get("/api/tasks?status=pending")
        assert r.status_code == 200, r.text
        assert _ids(r.json()) == ["t_pending"]


# ---------------------------------------------------------------------------
# created_by=<agent_id>
# ---------------------------------------------------------------------------


async def test_created_by_exact(tmp_path) -> None:
    """``?created_by=alice`` → only tasks alice created."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_alice_1", created_by="alice")
        _seed_task("t_alice_2", created_by="alice")
        _seed_task("t_bob_1", created_by="bob")

        r = admin.client.get("/api/tasks?created_by=alice")
        assert r.status_code == 200, r.text
        assert _ids(r.json()) == ["t_alice_1", "t_alice_2"]


# ---------------------------------------------------------------------------
# assigned=true  (complement of unassigned=true)
# ---------------------------------------------------------------------------


async def test_assigned_true_returns_only_assigned(tmp_path) -> None:
    """``?assigned=true`` → only tasks with an assignee."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_assigned_1", assigned_to="alice")
        _seed_task("t_assigned_2", assigned_to="bob")
        _seed_task("t_unassigned", assigned_to=None)

        r = admin.client.get("/api/tasks?assigned=true")
        assert r.status_code == 200, r.text
        assert _ids(r.json()) == ["t_assigned_1", "t_assigned_2"]


async def test_unassigned_true_guard(tmp_path) -> None:
    """``?unassigned=true`` still returns only NULL-assignee tasks."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_assigned", assigned_to="alice")
        _seed_task("t_unassigned", assigned_to=None)

        r = admin.client.get("/api/tasks?unassigned=true")
        assert r.status_code == 200, r.text
        assert _ids(r.json()) == ["t_unassigned"]


# ---------------------------------------------------------------------------
# Combined (AND) + back-compat
# ---------------------------------------------------------------------------


async def test_assigned_and_status_incomplete_combined(tmp_path) -> None:
    """``?assigned=true&status=incomplete`` → assigned AND non-terminal."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_assigned_open", assigned_to="alice", status="in_progress")
        _seed_task("t_assigned_done", assigned_to="alice", status="completed")
        _seed_task("t_unassigned_open", assigned_to=None, status="pending")

        r = admin.client.get("/api/tasks?assigned=true&status=incomplete")
        assert r.status_code == 200, r.text
        assert _ids(r.json()) == ["t_assigned_open"]


async def test_no_params_returns_all(tmp_path) -> None:
    """No query params → every task row (back-compat, bounded by limit)."""
    async with mcp_session(tmp_path) as admin:
        _seed_task("t_a", assigned_to="alice", status="completed")
        _seed_task("t_b", assigned_to=None, status="pending")
        _seed_task("t_c", created_by="bob", status="failed")

        r = admin.client.get("/api/tasks")
        assert r.status_code == 200, r.text
        assert _ids(r.json()) == ["t_a", "t_b", "t_c"]
