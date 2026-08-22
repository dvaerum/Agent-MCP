"""OBS-R12-2: DB-level terminal-state guard trigger (migration 0025) —
tool-layer regression coverage.

The DB-level guard (see ``tests/test_migration_0025_terminal_task_guard.py``
for the trigger-level RED/GREEN pair and
``tests/test_task_repository.py``/``tests/test_sqlalchemy_task_note.py``
for the repository-layer translation) sits UNDERNEATH every existing
Python-level check in ``tools/task_tools.py``. Adding it surfaced one
real regression during investigation: ``_update_single_task``'s
parent-notification write (appending "Subtask X status changed" to the
PARENT's notes) never checked whether the PARENT was already terminal —
so it silently succeeded pre-fix even when the parent was terminal, and
the new DB trigger would otherwise turn that into an uncaught
``TerminalTaskWriteBlocked`` on every FUTURE child completion under an
already-finished parent. This file pins the fix (skip the append when
the parent is terminal) plus the exception-translation wrapping added
alongside the trigger.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_mcp.db.connection import get_db_connection
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _task_row(task_id: str) -> dict:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


async def test_child_completion_succeeds_when_parent_already_terminal(
    tmp_path: Path,
) -> None:
    """A child task completing under an ALREADY-terminal parent must not
    fail — the parent-notify "FYI" note append is best-effort and must
    be skipped, not let an uncaught DB-guard exception turn a legitimate
    child completion into a failed whole request."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        r = admin.post(
            "/api/tasks",
            json={"task_title": "parent", "task_description": "..."},
        )
        assert r.status_code == 200, r.text
        parent_id = r.json()["task_id"]

        r = admin.post(
            "/api/tasks",
            json={
                "task_title": "child",
                "task_description": "...",
                "assigned_to": alice.agent_id,
                "parent_task": parent_id,
            },
        )
        assert r.status_code == 200, r.text
        child_id = r.json()["task_id"]

        # Drive the PARENT terminal directly (e.g. an admin cancelled
        # the epic while a child was still in flight) — no note append
        # is attempted here since this write doesn't touch a parent's
        # notes.
        await admin.call(
            "update_task_status",
            {"task_id": parent_id, "status": "cancelled"},
        )
        assert _task_row(parent_id)["status"] == "cancelled"

        # The CHILD's own completion must still succeed — its own
        # status transition (pending/in_progress -> completed) is
        # perfectly legal; only the parent-notify side effect touches
        # the (terminal) parent's notes.
        result = await alice.call(
            "update_task_status",
            {"task_id": child_id, "status": "completed"},
        )
        text = result[0].text if result else ""
        assert not getattr(alice, "_last_is_error", False), text
        assert "error" not in text.lower(), text
        assert _task_row(child_id)["status"] == "completed"

        # The parent's own status/notes are untouched by the child's
        # completion (the guard correctly refused the FYI append; the
        # code skips it rather than raising).
        parent_row = _task_row(parent_id)
        assert parent_row["status"] == "cancelled"


async def test_child_completion_still_notifies_live_parent(
    tmp_path: Path,
) -> None:
    """Regression guard for the fix above: a LIVE (non-terminal) parent
    must still receive the "Subtask ... status changed" note — the skip
    is conditional on the parent being terminal, not unconditional."""
    import json

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        r = admin.post(
            "/api/tasks",
            json={"task_title": "parent-live", "task_description": "..."},
        )
        parent_id = r.json()["task_id"]

        r = admin.post(
            "/api/tasks",
            json={
                "task_title": "child-live",
                "task_description": "...",
                "assigned_to": alice.agent_id,
                "parent_task": parent_id,
            },
        )
        child_id = r.json()["task_id"]

        await alice.call(
            "update_task_status",
            {"task_id": child_id, "status": "completed"},
        )

        parent_notes = json.loads(_task_row(parent_id)["notes"] or "[]")
        assert any(
            child_id in (n.get("content") or "") for n in parent_notes
        ), f"expected a subtask-completed note on the live parent, got {parent_notes}"
