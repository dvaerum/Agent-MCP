"""BL-R29-1 — system-driven dependency auto-advance must NOT be gated on
the completing caller's identity.

When a task completes, the system auto-advances its now-unblocked
dependents (BL-R26-1) and wakes their owners. That internal transition
runs via ``_update_single_task`` with the COMPLETER's identity, so the
per-row ownership gate silently drops the advance when the dependent is
owned by a DIFFERENT agent: the dependent never leaves ``pending``, its
owner is never woken, and the completer gets no error.

This suite pins the cross-agent advance on the single, bulk, and
child-cascade paths, and keeps the regression guard that a worker still
cannot DIRECTLY drive another agent's task through the normal path. RED
on origin/main (the advance is filtered out as a not-found failure).
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(
    title: str,
    assigned_to: str | None,
    *,
    status: str = "pending",
    depends_on: list[str] | None = None,
    parent_task: str | None = "__auto__",
    child_tasks: list[str] | None = None,
) -> str:
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection
    from tests.conftest import existing_root_task_id

    # R15-BL-1: default chains under the single root (first seed = root,
    # rest are children). An explicit parent_task= (incl. None) is honored.
    if parent_task == "__auto__":
        parent_task = existing_root_task_id()

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()
    deps = json.dumps(depends_on or [])
    kids = json.dumps(child_tasks or [])

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, notes, "
        "depends_on_tasks, parent_task, child_tasks) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, title, "seed", status, "low",
            assigned_to, "admin", now, now, "[]", deps, parent_task, kids,
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
        "parent_task": parent_task,
        "child_tasks": child_tasks or [],
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
# RED — single path: worker completes own blocker; cross-agent dependent
#        advances + its owner is woken
# ==========================================================================


async def test_single_complete_advances_cross_agent_dependent(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")

        t1 = _seed_task("T1", "alice", status="in_progress")
        t2 = _seed_task("T2", "bob", status="pending", depends_on=[t1])

        woken: list[str] = []
        from agent_mcp.core import globals as g
        monkeypatch.setattr(
            g, "notify_agent_inbox", lambda aid: woken.append(aid),
            raising=False,
        )

        # Worker alice (NOT admin) completes her own blocker T1.
        await alice.call(
            "update_task_status",
            {"task_id": t1, "status": "completed"},
        )

        assert _db_status(t1) == "completed"
        assert _db_status(t2) == "in_progress", (
            "cross-agent dependent T2 (owned by bob) must be auto-advanced "
            f"to in_progress; got {_db_status(t2)}"
        )
        assert "bob" in woken, (
            f"dependent owner 'bob' must be woken; got {woken}"
        )


# ==========================================================================
# RED — bulk path variant
# ==========================================================================


async def test_bulk_complete_advances_cross_agent_dependent(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")

        t1 = _seed_task("T1", "alice", status="in_progress")
        t2 = _seed_task("T2", "bob", status="pending", depends_on=[t1])

        woken: list[str] = []
        from agent_mcp.core import globals as g
        monkeypatch.setattr(
            g, "notify_agent_inbox", lambda aid: woken.append(aid),
            raising=False,
        )

        await alice.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_status", "task_id": t1, "status": "completed"},
            ]},
        )

        assert _db_status(t1) == "completed"
        assert _db_status(t2) == "in_progress", (
            "cross-agent dependent T2 (owned by bob) must be auto-advanced "
            f"on the bulk path; got {_db_status(t2)}"
        )
        assert "bob" in woken, (
            f"dependent owner 'bob' must be woken on bulk path; got {woken}"
        )


# ==========================================================================
# RED — cascade-to-children variant across agents
# ==========================================================================


async def test_cascade_to_children_crosses_agents(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")

        # Parent P owned by alice; child C owned by bob.
        # Seed P first without child, then C, then patch P's child list.
        parent = _seed_task("P", "alice", status="in_progress")
        child = _seed_task(
            "C", "bob", status="in_progress", parent_task=parent
        )
        # Wire the parent's child_tasks to include C (both DB + cache).
        from agent_mcp.core import globals as g
        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        conn.execute(
            "UPDATE tasks SET child_tasks = ? WHERE task_id = ?",
            (json.dumps([child]), parent),
        )
        conn.commit()
        conn.close()
        g.tasks[parent]["child_tasks"] = [child]

        await alice.call(
            "update_task_status",
            {
                "task_id": parent,
                "status": "cancelled",
                "cascade_to_children": True,
            },
        )

        assert _db_status(parent) == "cancelled"
        assert _db_status(child) == "cancelled", (
            "cross-agent child C (owned by bob) must be cascade-cancelled; "
            f"got {_db_status(child)}"
        )


# ==========================================================================
# GREEN-STAYS — regression guard: a worker still cannot DIRECTLY drive
#               another agent's task through the normal path
# ==========================================================================


async def test_worker_still_cannot_directly_update_foreign_task(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        # Task owned by alice.
        t = _seed_task("owned-by-alice", "alice", status="in_progress")

        # Bob (not assigned, not admin) tries to complete it directly.
        await bob.call(
            "update_task_status",
            {"task_id": t, "status": "completed"},
        )

        assert _db_status(t) != "completed", (
            "bob (not assigned) must NOT be able to directly drive alice's "
            "task via the normal update_task_status path"
        )
