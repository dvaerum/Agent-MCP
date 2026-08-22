"""R12-F4 — ``bulk_task_operations`` terminal-sink miss on
``update_priority`` / ``add_note``.

Sibling ops in the SAME function already carve out terminal tasks
before writing:

  * ``update_status`` calls ``_is_status_transition_allowed`` and
    denies (per-op error, ``continue``) a transition on a terminal
    task.
  * ``reassign`` (BL-R25-1) checks ``current_status in
    _TERMINAL_TASK_STATUSES`` and denies a reassign onto a live agent.

``update_priority`` and ``add_note`` never checked the task's current
status at all — they called ``_task_repo.update_fields`` unconditionally.
So a completed/cancelled/failed task's priority could be silently
changed, or a note silently appended, via the bulk surface — even
though the identical single-task path (``update_task`` dashboard route,
routed through ``_update_single_task``) correctly refuses EVERY
admin-field edit on a terminal task with a 409 "... is not allowed
(... is a terminal state)".

Fix: mirror the reassign sibling — read the task's CURRENT status
before writing and deny (per-op error + ``continue``, not aborting the
batch) when it's terminal.

RED on origin/main: both ops silently succeed on a terminal task and
the DB read-back shows the field actually changed. GREEN after the fix:
both ops are refused (per-op error, not the whole batch), and the DB
read-back is unchanged.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_terminal_task(
    title: str, *, status: str, priority: str = "low",
    assigned_to: str = "alice",
) -> str:
    """Insert a task row already pinned to a terminal status + mirror
    it into the in-memory cache. Mirrors the R25 bulk-reassign seed."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection
    from tests.conftest import existing_root_task_id

    parent = existing_root_task_id()
    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, notes, parent_task) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, title, "seed description", status, priority,
            assigned_to, "admin", now, now, "[]", parent,
        ),
    )
    conn.commit()
    conn.close()

    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "status": status,
        "priority": priority,
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "notes": [],
    }
    return task_id


def _db_task(task_id: str) -> dict:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status, priority, notes FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"task {task_id} vanished"
    return dict(row)


# ==========================================================================
# RED — update_priority on a TERMINAL task is denied, not silently applied
# ==========================================================================


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled", "failed"])
async def test_bulk_update_priority_on_terminal_task_denied(
    tmp_path, terminal_status: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_terminal_task(
            "terminal priority target", status=terminal_status,
            priority="low",
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_priority", "task_id": task_id,
                 "priority": "high"},
            ]},
        )
        text = result[0].text

        assert "terminal" in text.lower(), text
        assert "priority updated to 'high'" not in text, text

        row = _db_task(task_id)
        assert row["priority"] == "low", text
        assert row["status"] == terminal_status, text


# ==========================================================================
# RED — add_note on a TERMINAL task is denied, not silently applied
# ==========================================================================


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled", "failed"])
async def test_bulk_add_note_on_terminal_task_denied(
    tmp_path, terminal_status: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_terminal_task(
            "terminal note target", status=terminal_status,
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "add_note", "task_id": task_id,
                 "content": "should not land"},
            ]},
        )
        text = result[0].text

        assert "terminal" in text.lower(), text
        assert "Note added" not in text, text

        row = _db_task(task_id)
        assert row["notes"] in ("[]", None), text
        assert row["status"] == terminal_status, text


# ==========================================================================
# Regression — non-terminal tasks still allow both ops
# ==========================================================================


@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_bulk_update_priority_on_nonterminal_task_succeeds(
    tmp_path, status: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_terminal_task(
            "live priority target", status=status, priority="low",
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_priority", "task_id": task_id,
                 "priority": "high"},
            ]},
        )
        text = result[0].text
        assert "priority updated to 'high'" in text, text
        assert _db_task(task_id)["priority"] == "high", text


@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_bulk_add_note_on_nonterminal_task_succeeds(
    tmp_path, status: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_terminal_task("live note target", status=status)

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "add_note", "task_id": task_id,
                 "content": "processed"},
            ]},
        )
        text = result[0].text
        assert "Note added" in text, text
        assert _db_task(task_id)["notes"] != "[]", text


# ==========================================================================
# Regression — the batch is not aborted: a denied terminal op does not
# stop a later legitimate op in the same call
# ==========================================================================


async def test_bulk_batch_not_aborted_by_terminal_priority_or_note(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        terminal_id = _seed_terminal_task("done", status="completed")
        live_id = _seed_terminal_task("live", status="in_progress")

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_priority", "task_id": terminal_id,
                 "priority": "high"},
                {"type": "add_note", "task_id": live_id,
                 "content": "still processed"},
            ]},
        )
        text = result[0].text

        assert "terminal" in text.lower(), text
        assert "Note added" in text, text
        assert _db_task(terminal_id)["priority"] == "low", text
