"""BL-R17-1: dashboard clear-assignment must carve out TERMINAL tasks.

Regression from BL-R16-1. The dashboard clear-assignment branch
(``POST /api/update-task-dashboard`` with ``assigned_to`` null / '' /
'unassigned') UNCONDITIONALLY flipped ``status='unassigned'`` and fired
``g.notify_unassigned_task_appeared`` when clearing a task's assignee.

That RESURRECTS terminal work: a ``completed`` / ``cancelled`` /
``failed`` task whose assignee is cleared (or edited) by an operator was
flipped back to ``unassigned`` and fanned out as claimable — so an idle
worker re-executes already-done work. It also bypassed the BL-R12-1
terminal-sink transition guard (which correctly 409s a direct
``completed -> unassigned`` status write via the canonical path) and
missed the terminal carve-out the canonical unassign producers enforce:

  * agent-terminate (tools/admin_tools.py) — ``status NOT IN (terminal)``
  * REST create-unassigned (app/routers/tasks.py) — only fresh tasks

The fix mirrors those producers: when clearing an assignment, ONLY
transition status -> 'unassigned' AND fire the unassigned fanout if the
task's CURRENT status is NOT terminal
(``{'completed','cancelled','failed'}``). A terminal task may still have
its ``assigned_to`` cleared, but keeps its terminal status and fires NO
unassigned notify.

RED on origin/main (terminal task -> 'unassigned' + notify fires =
resurrection); GREEN after the carve-out. The non-terminal BL-R16-1
behavior (pending / in_progress clear -> 'unassigned' + notify) is
preserved.
"""

from __future__ import annotations

import pytest

import agent_mcp.core.globals as _g_mod
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


def _force_status(task_id: str, status: str, assigned_to: str) -> None:
    """Test setup: pin a task to a terminal status while keeping it
    assigned, bypassing the transition guard (a completed task can still
    carry ``assigned_to`` — the terminate producer's carve-out only
    exists because terminal tasks retain their assignee)."""
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
    body = {"task_title": "r17-clear-probe"}
    body.update(body_extra)
    r = admin.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _install_spies(monkeypatch):
    unassigned: list[tuple] = []
    notified: list[str] = []
    monkeypatch.setattr(
        _g_mod, "notify_unassigned_task_appeared",
        lambda task_id: unassigned.append(task_id),
    )
    monkeypatch.setattr(
        _g_mod, "notify_agent_inbox",
        lambda agent_id: notified.append(agent_id),
    )
    return unassigned, notified


def _clear_assignment(admin, task_id: str, value):
    return admin.post(
        "/api/update-task-dashboard",
        json={
            "task_id": task_id,
            "assigned_to": value,
        },
    )


# ===================== terminal carve-out (RED) ======================= #


TERMINAL_STATUSES = ["completed", "cancelled", "failed"]


@pytest.mark.parametrize("terminal_status", TERMINAL_STATUSES)
async def test_clear_assignment_keeps_terminal_status(
    tmp_path, terminal_status,
) -> None:
    """Clearing a TERMINAL task's assignee must NOT flip it back to
    'unassigned' — that would resurrect already-finished work."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, terminal_status, "alice")

        r = _clear_assignment(admin, task_id, None)
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["status"] == terminal_status, (
            f"clearing a {terminal_status!r} task's assignee must keep its "
            f"terminal status, got {row['status']!r} (resurrection)"
        )


@pytest.mark.parametrize("terminal_status", TERMINAL_STATUSES)
async def test_clear_assignment_terminal_does_not_fire_unassigned(
    tmp_path, monkeypatch, terminal_status,
) -> None:
    """Clearing a TERMINAL task's assignee must NOT fan out
    ``unassigned_task_appeared`` — no worker should be woken to re-run
    finished work."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, terminal_status, "alice")

        unassigned, _notified = _install_spies(monkeypatch)

        r = _clear_assignment(admin, task_id, None)
        assert r.status_code == 200, r.text

        fired_ids = list(unassigned)
        assert task_id not in fired_ids, (
            f"clearing a {terminal_status!r} task must NOT fire "
            f"notify_unassigned_task_appeared; fired={unassigned}"
        )


# ================== non-terminal preserved (BL-R16-1) ================= #


@pytest.mark.parametrize("non_terminal", ["pending", "in_progress"])
async def test_clear_assignment_nonterminal_still_unassigns(
    tmp_path, monkeypatch, non_terminal,
) -> None:
    """Regression guard: a PENDING / IN_PROGRESS task's cleared assignment
    still flips status -> 'unassigned' AND fires the fanout (the BL-R16-1
    behavior that must be preserved)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, non_terminal, "alice")

        unassigned, _notified = _install_spies(monkeypatch)

        r = _clear_assignment(admin, task_id, None)
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] is None, "assignment must be cleared"
        assert row["status"] == "unassigned", (
            f"non-terminal cleared task must become 'unassigned', got "
            f"{row['status']!r}"
        )
        fired_ids = list(unassigned)
        assert task_id in fired_ids, (
            f"non-terminal clear must fire the unassigned fanout; "
            f"fired={unassigned}"
        )
