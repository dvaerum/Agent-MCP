"""arch-r4 #1: REST update-task-dashboard dispatches through the
canonical ``update_task`` MCP tool instead of hand-reimplementing the
task-mutation invariant surface.

Background: ``update_task_details_api_route``
(``app/routers/composition.py``) used to write ``tasks``/``agents``
directly for every field EXCEPT ``status`` (which already routed
through the ``update_task_status`` tool). The other five fields
(title, description, priority, assigned_to, notes) hand-reimplemented
the terminal-sink transition guard, assignability, capability-routing,
and ``current_task`` reconcile invariants inline — a documented
drift-bug ledger (BL-R7-1, BL-R12-1, BL-R13-1, BL-R16-1, BL-R17-1,
BL-R18-1, BL-R30-1, AZ-R26-1) of cases where the duplicate fell out of
parity with the canonical path.

The fix: a new ``update_task`` tool
(``agent_mcp.tools.task_tools.update_task_tool_impl``) wraps the SAME
``_update_single_task`` helper ``update_task_status`` already uses; the
REST route collapses to a thin adapter (sanitize → dispatch → map
``ToolResult`` → HTTP), same shape as ``create_task_api_route``.

Test 1 pins the concrete NEW invariant this collapse introduces: since
``_update_single_task`` unconditionally re-validates the (possibly
unchanged) status on every admin-field write, a TERMINAL task
(completed/cancelled/failed) now refuses ANY admin-field edit routed
through this tool — not just an explicit status write or a reassign
(the pre-refactor route's narrower guards). RED on pre-refactor code
(a title-only edit on a completed task silently succeeded, 200); GREEN
after (rejected, non-2xx, title unchanged).

Test 2 is the parity suite: the terminal-sink / assignability /
capability-routing / current_task-reconcile invariants, each exercised
ONCE against the canonical tool, parametrized over {MCP call, REST
call} — the thing that was previously untestable as ONE invariant
surface because the REST path carried its own duplicate implementation.
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


def _current_task(agent_id: str) -> str | None:
    row = _row("agents", "agent_id = ?", (agent_id,))
    return row["current_task"] if row else None


def _force_status(task_id: str, status: str, assigned_to) -> None:
    """Test setup: pin a task to a status while keeping/setting its
    assignee, bypassing the transition guard (mirrors the sibling
    round-17/18 tests)."""
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
    from tests.conftest import ensure_seed_root

    # R15-BL-1: chain each probe under one dedicated root unless the
    # caller set a parent — parentless roots collide under the
    # single-root invariant, and these tests create several independent
    # tasks.
    body = {"task_title": "arch-r4-1-probe", "parent_task": ensure_seed_root()}
    body.update(body_extra)
    r = admin.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


# ============================================================== #
# Test 1: RED/GREEN — terminal-task admin-field edit now blocked #
# ============================================================== #


async def test_title_edit_on_terminal_task_rejected_via_rest(tmp_path) -> None:
    """A title-only edit (no status, no reassign) on a COMPLETED task
    must be rejected now that it routes through ``_update_single_task``
    — the SAME terminal-sink guard ``update_task_status`` already
    enforces applies uniformly to every admin field, not just status
    writes and reassigns.

    RED on the pre-refactor inline-write route (title updated, 200);
    GREEN after routing through the canonical ``update_task`` tool
    (rejected, non-2xx, title unchanged).
    """
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)
        _force_status(task_id, "completed", None)

        r = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "title": "renamed after completion",
            },
        )
        assert r.status_code != 200, (
            f"editing a completed task's title must be rejected (terminal "
            f"is a sink), got {r.status_code}: {r.text}"
        )

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["title"] != "renamed after completion", (
            "a rejected terminal-task edit must not land in the DB"
        )
        assert row["status"] == "completed"


async def test_title_edit_on_active_task_still_succeeds(tmp_path) -> None:
    """Regression: the SAME title-only edit on a non-terminal task keeps
    working (only terminal tasks are newly guarded)."""
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)

        r = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "title": "renamed while active",
            },
        )
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["title"] == "renamed while active"


async def test_null_valued_editable_field_alone_is_harmless_noop(tmp_path) -> None:
    """Regression guard for a dispatch-layer edge case surfaced during
    review: ``dispatch_tool_call``'s schema-cleaning step strips ANY
    top-level ``null`` argument (the Q6e ``token: null`` tolerance rule
    applies uniformly, not just to ``token``) before the ``update_task``
    tool ever sees it. A body like ``{"task_id": t, "status": null}``
    has ``"status"`` present in the RAW JSON (passes the route's
    wire-level "at least one editable field" gate, matching the
    pre-refactor route) but arrives at the tool with NO editable keys
    at all. The tool must treat this as the SAME harmless no-op 200 the
    pre-refactor route returned — not a 400 (an earlier draft of the
    tool re-checked key PRESENCE post-dispatch and wrongly 400'd this).
    """
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)

        r = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "status": None,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True


# ============================================================== #
# Test 2: parity suite — {MCP, REST} against the SAME invariants #
# ============================================================== #


async def _update_via(surface: str, admin, task_id: str, **fields):
    """Dispatch an update through either the REST route or the MCP
    ``update_task`` tool. Returns ``(ok, detail)`` where ``ok`` is True
    iff the surface reports success."""
    if surface == "rest":
        body = {"task_id": task_id, **fields}
        r = admin.post("/api/update-task-dashboard", json=body)
        return r.status_code == 200, f"{r.status_code}: {r.text}"

    assert surface == "mcp"
    result = await admin.call("update_task", {"task_id": task_id, **fields})
    text = result[0].text if result else ""
    is_error = bool(getattr(admin, "_last_is_error", False))
    ok = not is_error and not text.lower().startswith(("error", "unauthorized"))
    return ok, text


@pytest.mark.parametrize("surface", ["mcp", "rest"])
async def test_update_task_invariants_parity(tmp_path, surface) -> None:
    """The terminal-sink / assignability / current_task-reconcile
    invariants, exercised ONCE against the canonical ``update_task``
    tool, over BOTH call surfaces. Before arch-r4 #1 this was
    untestable as a single invariant surface — the REST path carried
    its own duplicate implementation that could (and repeatedly did)
    drift from the MCP tool's.

    (The capability-routing invariant that used to be exercised here
    was retired in PR5 — the structured capability-tag gate is gone.)
    """
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        await admin.create_worker("carol")

        # --- assignability (BL-R13-1): reassign to a nonexistent agent.
        task_assign = await _create_task(admin, assigned_to="alice")
        ok, detail = await _update_via(
            surface, admin, task_assign, assigned_to="ghost-does-not-exist",
        )
        assert not ok, f"reassign to a nonexistent agent must be rejected: {detail}"
        assert _row("tasks", "task_id = ?", (task_assign,))["assigned_to"] == "alice"

        # --- terminal-sink (BL-R12-1 / BL-R18-1): a completed task must
        # refuse a reassign to a live agent.
        task_terminal = await _create_task(admin, assigned_to="alice")
        _force_status(task_terminal, "completed", "alice")
        ok, detail = await _update_via(
            surface, admin, task_terminal, assigned_to="carol",
        )
        assert not ok, f"reassigning a completed task must be rejected: {detail}"
        row = _row("tasks", "task_id = ?", (task_terminal,))
        assert row["status"] == "completed"
        assert row["assigned_to"] == "alice"

        # --- current_task reconcile (BL-R30-1): a plain reassign moves
        # BOTH the losing and gaining agent's current_task pointer.
        task_reconcile = await _create_task(admin, assigned_to="alice")
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE agents SET current_task = ? WHERE agent_id = ?",
                (task_reconcile, "alice"),
            )
            conn.commit()
        finally:
            conn.close()

        ok, detail = await _update_via(
            surface, admin, task_reconcile, assigned_to="bob",
        )
        assert ok, f"reassign to a live, capable-enough agent must succeed: {detail}"
        assert _current_task("alice") is None, (
            "losing agent's current_task must be cleared on reassign"
        )
        assert _current_task("bob") == task_reconcile, (
            "gaining agent's current_task must be set on reassign"
        )
