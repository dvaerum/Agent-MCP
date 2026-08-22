"""R12-F5 — ``update_task`` clearing branch never carves out TERMINAL
tasks, unlike every other admin field it guards.

Background (arch-r4 #1 / BL-R16-1 / BL-R17-1): ``update_task_tool_impl``
routes every admin field (title, description, priority, notes, explicit
status, reassign-to-a-live-agent) through ``_update_single_task``, which
re-validates the task's CURRENT status via ``_is_status_transition_
allowed`` on every write — so a terminal (completed/cancelled/failed)
task refuses ALL of those fields. ``assigned_to`` CLEARING (null / "" /
"unassigned") is a dedicated code path OUTSIDE that helper (BL-R16-1 /
BL-R17-1 choreography: flip to 'unassigned' + fan out only on a
NON-terminal task) — but that path builds ``clear_fields =
{"assigned_to": None}`` and writes it UNCONDITIONALLY; only the
ADDITIONAL ``status`` flip is gated on the task being non-terminal.

So a bare ``update_task(task_id, assigned_to=None)`` — no other field —
on an ALREADY-terminal task never touches ``_update_single_task`` at
all (``admin_fields_requested`` is False), skips the terminal-sink guard
entirely, and silently strips ``assigned_to`` from finished work — the
one admin-adjacent field the terminal-sink guard doesn't gate, breaking
the function's own stated invariant that a terminal task refuses every
admin-field edit.

The combined-call case (``{"status": "completed", "assigned_to": null}``
in ONE call, driving a task terminal and clearing it at the same time)
is a DIFFERENT, already-tested, intentionally-preserved shape (see the
module docstring in ``task_tools.py`` around ``clearing_fanout_needed``)
and must keep working — the guard here keys on the task's status BEFORE
this call (``prior_status``), not the effective post-call status, so it
never fires when a call is itself the one completing the task.

RED on origin/main: the clearing-only call on an already-terminal task
succeeds and ``assigned_to`` is stripped. GREEN after the fix: the call
is refused and ``assigned_to`` is untouched.
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
    from tests.conftest import ensure_seed_root

    body = {"task_title": "r12-f5-clear-probe", "parent_task": ensure_seed_root()}
    body.update(body_extra)
    r = admin.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _force_status(task_id: str, status: str, assigned_to) -> None:
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


TERMINAL_STATUSES = ["completed", "cancelled", "failed"]


# ==========================================================================
# RED — bare assigned_to:null clear on a TERMINAL task via the MCP tool
# ==========================================================================


@pytest.mark.parametrize("terminal_status", TERMINAL_STATUSES)
async def test_mcp_clear_assignment_on_terminal_task_denied(
    tmp_path, terminal_status: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, terminal_status, "alice")

        # "unassigned" (not a raw None): dispatch_tool_call's schema
        # cleaning strips ANY top-level null argument as absent (the
        # Q6e tolerance rule — see the composition.py route's own
        # normalization comment), so a bare ``assigned_to: null`` sent
        # straight over the MCP wire never reaches the tool as a clear
        # intent at all. Real callers spell "clear" as "unassigned" /
        # "" for exactly this reason; mirror that here.
        result = await admin.call(
            "update_task", {"task_id": task_id, "assigned_to": "unassigned"},
        )
        text = result[0].text if result else ""
        is_error = bool(getattr(admin, "_last_is_error", False))

        assert is_error or "terminal" in text.lower(), text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] == "alice", (
            f"clearing assigned_to on a {terminal_status!r} task must be "
            f"refused, not silently stripped: {text}"
        )
        assert row["status"] == terminal_status, text


# ==========================================================================
# RED — same via the REST dashboard adapter (thin wrapper over the tool)
# ==========================================================================


@pytest.mark.parametrize("terminal_status", TERMINAL_STATUSES)
async def test_rest_clear_assignment_on_terminal_task_denied(
    tmp_path, terminal_status: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, terminal_status, "alice")

        r = admin.post(
            "/api/update-task-dashboard",
            json={"task_id": task_id, "assigned_to": None},
        )
        assert r.status_code != 200, (
            f"clearing assigned_to on a {terminal_status!r} task must be "
            f"refused, got {r.status_code}: {r.text}"
        )

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] == "alice", r.text
        assert row["status"] == terminal_status, r.text


# ==========================================================================
# Regression — clearing on a NON-terminal task still works exactly as
# before (BL-R16-1: status -> 'unassigned', assigned_to cleared)
# ==========================================================================


@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_clear_assignment_on_nonterminal_task_still_succeeds(
    tmp_path, status: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, status, "alice")

        r = admin.post(
            "/api/update-task-dashboard",
            json={"task_id": task_id, "assigned_to": None},
        )
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] is None, r.text
        assert row["status"] == "unassigned", r.text


# ==========================================================================
# Regression — a combined {status: "completed", assigned_to: null} call
# in ONE request (the task itself becoming terminal + clearing at the
# same time) must keep working: the guard keys on the PRIOR status, not
# the post-call effective status.
# ==========================================================================


async def test_combined_complete_and_clear_in_one_call_still_succeeds(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        _force_status(task_id, "in_progress", "alice")

        r = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "status": "completed",
                "assigned_to": None,
            },
        )
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] is None, r.text
        assert row["status"] == "completed", r.text
