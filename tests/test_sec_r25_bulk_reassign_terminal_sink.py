"""BL-R25-1 — ``bulk_task_operations`` reassign terminal-sink guard.

The bulk ``reassign`` op (admin/manager-gated via ``is_admin_request``)
validated the TARGET agent (``_agent_assignable`` — exists + not
terminated) but never checked whether the TASK itself was in a terminal
status. So a manager-role MCP agent or an operator could bulk-``reassign``
a completed/cancelled/failed task onto a live agent — silently mutating a
finished task's ``assigned_to`` and effectively resurrecting it onto an
active worker.

This is the missed sibling at the intersection of two classes closed at
different times:

  * BL-R18-1 closed the terminal-sink on the ASSIGN axis for the MCP
    self-claim path, the single ``_update_single_task`` path, and the
    dashboard composition reassign (409).
  * AZ-R16-1 closed the bulk-vs-single privilege-parity guards for bulk
    ``update_status`` / ``update_priority`` — but NOT the terminal-sink
    on bulk ``reassign``.

Fix: before the bulk ``reassign`` op writes ``assigned_to``, read the
task's CURRENT status and deny the op (append a per-op error result +
``continue``, without mutating ``assigned_to``) when it's terminal —
mirroring the single-path / dashboard-composition shape. The rest of the
bulk transaction is unaffected (deny one op, don't abort the batch).

Tests assert against the DB directly (authoritative source), since the
in-memory cache and the tool's text result can diverge.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_assigned_task(
    title: str, assigned_to: str, *, priority: str = "low",
    status: str = "pending",
) -> str:
    """Insert a task row assigned to ``assigned_to`` + mirror it into
    the in-memory cache. Mirrors the R16 bulk-ops SEC tests."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    from tests.conftest import existing_root_task_id

    # R15-BL-1: chain under the single root (first seed = root, rest are
    # children). Bulk reassign keys on assigned_to, not parentage.
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
    """Read a task row straight from the DB (authoritative source)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status, assigned_to FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"task {task_id} vanished"
    return dict(row)


# ==========================================================================
# RED — admin cannot bulk-reassign a TERMINAL task onto a live agent
# ==========================================================================


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled", "failed"])
async def test_admin_bulk_reassign_terminal_task_denied(
    tmp_path, terminal_status: str,
) -> None:
    """An admin/operator (carries ``tasks.assign``) calling
    ``bulk_task_operations`` with a ``reassign`` op on a task whose
    status is terminal must be DENIED — the finished task must not be
    re-pinned onto a live agent.

    RED (origin/main): the bulk reassign op only checks the target via
    ``_agent_assignable`` and writes ``assigned_to``, so the terminal
    task is silently mutated / resurrected. GREEN: the op is denied
    (per-op error result) and ``assigned_to`` + ``status`` are
    unchanged."""
    async with mcp_session(tmp_path) as admin:
        # Live target agent the reassign would land on.
        await admin.create_worker("bob")
        task_id = _seed_assigned_task(
            "terminal target", "alice", status=terminal_status
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id,
                 "assigned_to": "bob"},
            ]},
        )
        text = result[0].text

        # Op must report a terminal-status refusal, not a success.
        assert "terminal" in text.lower(), text
        assert "reassigned to 'bob'" not in text, text

        # Authoritative source: ownership + status unchanged.
        row = _db_task(task_id)
        assert row["assigned_to"] == "alice", text
        assert row["status"] == terminal_status, text


# ==========================================================================
# Regression — non-terminal task reassign still succeeds
# ==========================================================================


@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_admin_bulk_reassign_nonterminal_task_succeeds(
    tmp_path, status: str,
) -> None:
    """The guard must only block terminal tasks — a pending / in_progress
    task reassigned to a live agent still succeeds."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")
        task_id = _seed_assigned_task(
            "live target", "alice", status=status
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id,
                 "assigned_to": "bob"},
            ]},
        )
        text = result[0].text

        assert "reassigned to 'bob'" in text, text
        row = _db_task(task_id)
        assert row["assigned_to"] == "bob", text
        assert row["status"] == status, text


# ==========================================================================
# Regression — reassign to a nonexistent target still fails first
# ==========================================================================


async def test_admin_bulk_reassign_nonexistent_target_still_denied(
    tmp_path,
) -> None:
    """The ``_agent_assignable`` target validation is unchanged — a
    reassign to an agent that doesn't exist is still refused (on a
    NON-terminal task, so the terminal guard isn't what trips it)."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_assigned_task(
            "bad target", "alice", status="pending"
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id,
                 "assigned_to": "ghost"},
            ]},
        )
        text = result[0].text

        assert "does not exist or is terminated" in text, text
        assert _db_task(task_id)["assigned_to"] == "alice", text


# ==========================================================================
# Regression — the batch is not aborted: a denied terminal reassign does
# not stop a later legitimate op in the same call
# ==========================================================================


async def test_bulk_batch_not_aborted_by_terminal_reassign(
    tmp_path,
) -> None:
    """A denied terminal-task reassign skips only that op — a following
    op in the same bulk call (add_note on a live task) still applies."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")
        terminal_id = _seed_assigned_task(
            "done", "alice", status="completed"
        )
        live_id = _seed_assigned_task(
            "live", "alice", status="in_progress"
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": terminal_id,
                 "assigned_to": "bob"},
                {"type": "add_note", "task_id": live_id,
                 "content": "still processed"},
            ]},
        )
        text = result[0].text

        # First op denied (terminal), second op applied.
        assert "terminal" in text.lower(), text
        assert "Note added" in text, text
        assert _db_task(terminal_id)["assigned_to"] == "alice", text
