"""BL-R26-1 — ``bulk_task_operations`` must run the reconcile/notify tail.

The canonical single ``update_task_status`` path runs a Phase-2/3/4 tail
after a successful mutation:

  * ``auto_update_dependencies`` — a status→completed unblocks any
    dependent whose dependencies are now all complete (pending →
    in_progress).
  * ``notify_agent_inbox`` — wakes each touched task's current assignee
    (the reassign target / dep-advanced owner) instead of leaving them
    idle until the next poll.
  * ``index_task_data`` — re-indexes the mutated task so RAG isn't stale.

``bulk_task_operations`` skipped all three. This suite pins them on the
bulk path. RED on origin/main.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(
    title: str, assigned_to: str | None, *, status: str = "pending",
    depends_on: list[str] | None = None,
) -> str:
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    from tests.conftest import existing_root_task_id

    # R15-BL-1: chain under the single root (first seed = root, rest are
    # children). Dependency-advance keys on depends_on/status, not parent.
    parent = existing_root_task_id()

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()
    deps = json.dumps(depends_on or [])

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, notes, "
        "depends_on_tasks, parent_task) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, title, "seed", status, "low",
            assigned_to, "admin", now, now, "[]", deps, parent,
        ),
    )
    conn.commit()
    conn.close()
    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "status": status,
        "priority": "low",
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "notes": [],
        "depends_on_tasks": depends_on or [],
    }
    return task_id


def _db_status(task_id: str) -> str:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"task {task_id} vanished"
    return row["status"]


# ==========================================================================
# RED — bulk status→completed advances an unblocked dependent
# ==========================================================================


async def test_bulk_complete_advances_dependent(tmp_path) -> None:
    """A bulk ``update_status`` → completed on task A must auto-advance a
    pending dependent B (depends_on == [A]) to in_progress, exactly as
    the single path does. RED on origin/main (bulk skips Phase-3)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        a = _seed_task("A", "alice", status="in_progress")
        b = _seed_task("B", "alice", status="pending", depends_on=[a])

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_status", "task_id": a, "status": "completed"},
            ]},
        )
        text = result[0].text
        assert "status updated to 'completed'" in text, text

        # Authoritative source: dependent advanced off pending.
        assert _db_status(a) == "completed", text
        assert _db_status(b) == "in_progress", (
            f"dependent B should be auto-advanced to in_progress; "
            f"got {_db_status(b)}. {text}"
        )


# ==========================================================================
# RED — bulk reassign wakes the target agent's inbox
# ==========================================================================


async def test_bulk_reassign_wakes_target(tmp_path, monkeypatch) -> None:
    """A bulk ``reassign`` must ``notify_agent_inbox`` the new assignee so
    an idle waiter wakes now, not on the next poll. RED on origin/main."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")
        task_id = _seed_task("t", "alice", status="pending")

        woken: list[str] = []
        from agent_mcp.core import globals as g
        monkeypatch.setattr(
            g, "notify_agent_inbox", lambda aid: woken.append(aid),
            raising=False,
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id, "assigned_to": "bob"},
            ]},
        )
        assert "reassigned to 'bob'" in result[0].text, result[0].text
        assert "bob" in woken, (
            f"reassign target 'bob' must be woken; got {woken}"
        )


# ==========================================================================
# RED — bulk status update re-indexes the mutated task
# ==========================================================================


async def test_bulk_status_reindexes(tmp_path, monkeypatch) -> None:
    """A bulk ``update_status`` must re-index the mutated task via
    ``index_task_data`` so RAG isn't stale. RED on origin/main."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = _seed_task("t", "alice", status="in_progress")

        indexed: list[str] = []

        async def _fake_index(tid, data):
            indexed.append(tid)

        import agent_mcp.tools.task_tools as tt
        monkeypatch.setattr(tt, "index_task_data", _fake_index, raising=True)

        await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_status", "task_id": task_id,
                 "status": "completed"},
            ]},
        )
        # Phase-4 reindex is fire-and-forget via asyncio.create_task; give
        # the scheduled task a slice to run.
        await asyncio.sleep(0.05)
        assert task_id in indexed, (
            f"bulk-mutated task should be re-indexed; got {indexed}"
        )
