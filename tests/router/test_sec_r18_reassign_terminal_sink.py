"""BL-R18-1 (reassign half): dashboard REASSIGN must treat TERMINAL as a sink.

Companion to BL-R17-1 (clear-assignment carve-out). The terminal-sink
invariant was enforced only on the task STATUS axis, never on the ASSIGN
axis. The dashboard task-edit REASSIGN branch
(``POST /api/update-task-dashboard`` with a NON-EMPTY ``assigned_to``)
only checked ``_agent_assignable`` (BL-R13-1 — target agent exists AND is
not terminated) but did NOT check whether the TASK itself is terminal.

Reassigning a ``completed`` / ``cancelled`` / ``failed`` task to a live
agent effectively RESURRECTS finished work into an active-work state on
the assign axis — the mirror of the status-axis resurrection the
BL-R12-1 / R16-1 / R17-1 guards close. Terminal must be a sink on the
ASSIGN axis too.

The fix rejects a reassign-to-a-real-agent of a terminal task with 409
Conflict (mirroring composition.py's status-path illegal-transition
guard, which 409s a terminal-source transition). It does NOT touch the
CLEAR-assignment branch (``assigned_to`` -> null / '') — that is
BL-R17-1's carve-out (a terminal task may still be unassigned, keeping
its terminal status).

RED on origin/main (reassign succeeds; task now re-pinned on a live
agent = resurrected). GREEN after the terminal-task check.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


TERMINAL_STATUSES = ["completed", "cancelled", "failed"]


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


def _force_status(task_id: str, status: str, assigned_to) -> None:
    """Test setup: pin a task to a status while keeping/setting its
    assignee, bypassing the transition guard."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET status = ?, assigned_to = ? WHERE task_id = ?",
            (status, assigned_to, task_id),
        )
        conn.commit()
    finally:
        conn.close()


async def _create_task(admin, **body_extra) -> str:
    body = {"task_title": "r18-reassign-probe"}
    body.update(body_extra)
    r = admin.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _reassign(admin, task_id: str, value):
    return admin.post(
        "/api/update-task-dashboard",
        json={
            "task_id": task_id,
            "assigned_to": value,
        },
    )


# ==================== terminal-sink on assign axis (RED) =============== #


@pytest.mark.parametrize("terminal_status", TERMINAL_STATUSES)
async def test_reassign_terminal_task_rejected(
    tmp_path, terminal_status,
) -> None:
    """Reassigning a TERMINAL task to a live agent must be REJECTED (409)
    and leave the task's status + assignment unchanged — reassigning it
    would resurrect finished work onto an active worker's queue."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, terminal_status, "alice")

        r = _reassign(admin, task_id, "bob")
        assert r.status_code == 409, (
            f"reassigning a {terminal_status!r} task to a live agent must be "
            f"rejected with 409, got {r.status_code}: {r.text}"
        )

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["status"] == terminal_status, (
            f"rejected reassign must keep {terminal_status!r} status, got "
            f"{row['status']!r} (resurrection)"
        )
        assert row["assigned_to"] == "alice", (
            f"rejected reassign must leave assignment unchanged, got "
            f"{row['assigned_to']!r}"
        )


# ==================== regression: non-terminal still works ============= #


@pytest.mark.parametrize("non_terminal", ["pending", "in_progress"])
async def test_reassign_nonterminal_task_still_succeeds(
    tmp_path, non_terminal,
) -> None:
    """A PENDING / IN_PROGRESS task may still be reassigned to a live
    agent (the BL-R13-1 behavior that must be preserved)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, non_terminal, "alice")

        r = _reassign(admin, task_id, "bob")
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] == "bob", "reassignment must take effect"


@pytest.mark.parametrize("terminal_status", TERMINAL_STATUSES)
async def test_clear_terminal_assignment_still_allowed(
    tmp_path, terminal_status,
) -> None:
    """BL-R17-1 carve-out must be untouched: CLEARING a terminal task's
    assignment (assigned_to -> '') still succeeds and keeps the terminal
    status (no error, no resurrection)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, terminal_status, "alice")

        r = _reassign(admin, task_id, "")
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["status"] == terminal_status, (
            f"clearing a {terminal_status!r} task's assignee must keep its "
            f"terminal status, got {row['status']!r}"
        )
        assert row["assigned_to"] is None, "assignment must be cleared"


@pytest.mark.parametrize("terminal_status", TERMINAL_STATUSES)
async def test_reassign_terminal_to_bad_agent_still_rejected(
    tmp_path, terminal_status,
) -> None:
    """BL-R13-1 must be untouched: reassigning to a nonexistent agent is
    still rejected (the assignability guard fires regardless of the
    terminal-task guard)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, terminal_status, "alice")

        r = _reassign(admin, task_id, "ghost-agent-does-not-exist")
        assert r.status_code in (400, 409), (
            f"reassigning to a nonexistent agent must be rejected, got "
            f"{r.status_code}: {r.text}"
        )

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] == "alice", "assignment must be unchanged"
