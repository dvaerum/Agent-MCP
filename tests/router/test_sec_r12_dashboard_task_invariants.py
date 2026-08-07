"""Round-12 (BL-R12-1): dashboard task-status must honour MCP invariants.

The REST ``POST /api/update-task-dashboard`` handler wrote task
``status`` directly via ``task_repo.update_fields``, bypassing the four
invariants the canonical MCP path (``update_task_status`` tool →
``_update_single_task``) enforces:

  1. terminal-state transition guard — a ``completed`` task could be
     resurrected to ``in_progress`` via the dashboard (HTTP 200) even
     though the MCP path refuses the write;
  2. ``clear_current_task_for`` — completing a task did NOT clear a
     pinned ``agents.current_task`` pointer, leaving a stale pointer
     that leaks into ``/api/all-data``;
  3. the parent subtask-completion note;
  4. status-enum validation.

The fix routes the dashboard's ``status`` change through the same
canonical ``update_task_status`` path. These tests pin invariants (1)
and (2) plus the legal-transition regression. RED on origin/main
(resurrection succeeds / pointer stays stale); GREEN after the fix.
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


async def _create_task(admin, **body_extra) -> str:
    body = {"task_title": "invariant-probe"}
    body.update(body_extra)
    r = admin.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _set_status(admin, task_id: str, status: str):
    return admin.post(
        "/api/update-task-dashboard",
        json={"task_id": task_id, "status": status},
    )


# ============ Invariant (1): terminal-state transition guard ============ #


async def test_dashboard_cannot_resurrect_completed_task(tmp_path) -> None:
    """A ``completed`` task must NOT be re-opened to ``in_progress`` via
    the dashboard — matching the MCP path's terminal-state sink rule."""
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)

        # Drive it to completion (pending -> completed is legal).
        r = _set_status(admin, task_id, "completed")
        assert r.status_code == 200, r.text
        assert _row("tasks", "task_id = ?", (task_id,))["status"] == "completed"

        # Attempt the resurrection.
        r2 = _set_status(admin, task_id, "in_progress")
        assert r2.status_code != 200, (
            f"resurrection of a completed task must be rejected, got "
            f"{r2.status_code}: {r2.text}"
        )

        # And the DB is authoritative: still completed, never resurrected.
        assert _row("tasks", "task_id = ?", (task_id,))["status"] == "completed"


async def test_dashboard_rejects_bogus_status_enum(tmp_path) -> None:
    """Invariant (4): an arbitrary status string is rejected, not stored."""
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)
        r = _set_status(admin, task_id, "totally-bogus-status")
        assert r.status_code != 200, r.text
        assert _row("tasks", "task_id = ?", (task_id,))["status"] != (
            "totally-bogus-status"
        )


# ========== Invariant (2): clear_current_task_for on completion ========= #


async def test_dashboard_completion_clears_current_task_pointer(tmp_path) -> None:
    """Completing a task via the dashboard must clear any agent's
    ``current_task`` pointer aimed at that task (the stale-pointer bug
    that leaks into ``/api/all-data``)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")

        # Pin alice's current_task at the task (what claim_task does).
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE agents SET current_task = ? WHERE agent_id = ?",
                (task_id, "alice"),
            )
            conn.commit()
        finally:
            conn.close()
        assert _row("agents", "agent_id = ?", ("alice",))["current_task"] == task_id

        # Complete via dashboard.
        r = _set_status(admin, task_id, "completed")
        assert r.status_code == 200, r.text

        # Pointer must be cleared.
        assert _row("agents", "agent_id = ?", ("alice",))["current_task"] is None, (
            "completing a task via the dashboard must clear the assignee's "
            "current_task pointer"
        )


# ============================ regression =============================== #


async def test_dashboard_legal_transition_still_succeeds(tmp_path) -> None:
    """A legal pending -> in_progress transition via the dashboard still
    lands (200, and the DB reflects it)."""
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)
        r = _set_status(admin, task_id, "in_progress")
        assert r.status_code == 200, r.text
        assert _row("tasks", "task_id = ?", (task_id,))["status"] == "in_progress"


async def test_dashboard_non_status_edit_still_succeeds(tmp_path) -> None:
    """A non-status field edit (title) keeps working (direct-write path)."""
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)
        r = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "title": "renamed via dashboard",
            },
        )
        assert r.status_code == 200, r.text
        assert _row("tasks", "task_id = ?", (task_id,))["title"] == (
            "renamed via dashboard"
        )
