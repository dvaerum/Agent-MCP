"""SEC-R19 — BL-R19-1: force_delete cascade must reconcile dangling
``depends_on_tasks`` refs on DESCENDANTS, not just the root.

BL-R19-1 (LOW-MED, data-integrity). ``force_delete`` cleaned dangling
``depends_on`` references pointing at the ROOT being deleted (for the
root's dependents) but NOT refs pointing at the cascade-deleted
DESCENDANTS. So an OUTSIDE task depending on a descendant that gets
cascade-deleted keeps the now-absent descendant id in its
``depends_on_tasks``; ``auto_update_dependencies`` (which only advances
a dependent when a dependency *completes*) never fires for a *deleted*
dependency, so the outside task stalls at ``pending`` forever — a
silent workflow stall. Same "delete must reconcile references" class
as BL-2 / BL-R4-1.

Fix: collect the FULL deleted set (root + ALL descendants) and
reconcile EVERY other task's ``depends_on_tasks`` to drop refs to any
id in that set, then re-evaluate so a task whose last blocking dep was
deleted can proceed (pending → in_progress).
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(
    *,
    title: str = "seeded task",
    status: str = "pending",
    assigned_to: str | None = None,
    parent_task: str | None = "__auto__",
    child_tasks: list | None = None,
    depends_on_tasks: list | None = None,
) -> str:
    """Insert a task row directly. Returns the task_id."""
    from agent_mcp.db.connection import get_db_connection
    from tests.conftest import ensure_seed_root

    # R15-BL-1: this suite needs INDEPENDENT subtrees (an "outside" task
    # must survive another subtree's cascade delete). Under the
    # single-root invariant those become SIBLINGS under one dedicated
    # hidden root — so the default parent is that root, never a
    # to-be-deleted seed. An explicit parent_task= is honored.
    if parent_task == "__auto__":
        parent_task = ensure_seed_root()

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, parent_task, "
        "child_tasks, depends_on_tasks) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            "test description",
            status,
            "medium",
            assigned_to,
            "admin",
            now,
            now,
            parent_task,
            json.dumps(child_tasks or []),
            json.dumps(depends_on_tasks or []),
        ),
    )
    conn.commit()
    conn.close()
    return task_id


def _task(task_id: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _depends_on(task_id: str) -> list:
    row = _task(task_id)
    return json.loads(row["depends_on_tasks"] or "[]") if row else []


# ── BL-R19-1: descendant-dep reconcile ───────────────────────────


async def test_force_delete_reconciles_descendant_dependency(tmp_path) -> None:
    """Delete root A (cascading child B); an outside task C depending on
    B must (a) no longer reference the deleted B in its deps and (b) be
    unblocked (advanced out of pending) rather than stalled forever."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        a = _seed_task(title="root A")
        b = _seed_task(title="child B", parent_task=a)
        # Keep A's mirror realistic (source of truth is the FK column).
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        conn.execute(
            "UPDATE tasks SET child_tasks = ? WHERE task_id = ?",
            (json.dumps([b]), a),
        )
        conn.commit()
        conn.close()

        # C is an OUTSIDE task (not under A) that depends on descendant B.
        c = _seed_task(
            title="outside C",
            status="pending",
            assigned_to=alice.agent_id,
            depends_on_tasks=[b],
        )

        res = await admin.call(
            "delete_task", {"task_id": a, "force_delete": True}
        )
        assert "deleted successfully" in res[0].text.lower(), res[0].text

        # Cascade happened: A and B are gone.
        assert _task(a) is None, "root A should be deleted"
        assert _task(b) is None, "descendant B should be cascade-deleted"

        # (a) C no longer references the deleted descendant B.
        assert b not in _depends_on(c), (
            "C's depends_on_tasks must not reference the cascade-deleted "
            f"descendant B; got {_depends_on(c)!r}"
        )
        # (b) C is unblocked — its last blocking dep was removed, so it
        # advanced out of pending rather than stalling forever.
        assert _task(c)["status"] == "in_progress", (
            "C should be auto-advanced (its only blocking dep was deleted); "
            f"got status={_task(c)['status']!r}"
        )


async def test_force_delete_still_cascades_and_leaves_unrelated_untouched(
    tmp_path,
) -> None:
    """Regression: force_delete still cascades the whole subtree, and an
    unrelated task's dependencies are left untouched."""
    async with mcp_session(tmp_path) as admin:
        a = _seed_task(title="root A")
        b = _seed_task(title="child B", parent_task=a)
        grandchild = _seed_task(title="grandchild B2", parent_task=b)

        # An unrelated dependency chain that must survive untouched.
        keep_dep = _seed_task(title="unrelated dep", status="completed")
        keep = _seed_task(
            title="unrelated dependent",
            status="pending",
            depends_on_tasks=[keep_dep],
        )

        res = await admin.call(
            "delete_task", {"task_id": a, "force_delete": True}
        )
        assert "deleted successfully" in res[0].text.lower(), res[0].text

        # Whole subtree gone.
        assert _task(a) is None
        assert _task(b) is None
        assert _task(grandchild) is None

        # Unrelated task untouched.
        assert _task(keep) is not None
        assert _depends_on(keep) == [keep_dep], (
            "unrelated task's deps must be untouched by the cascade; "
            f"got {_depends_on(keep)!r}"
        )
