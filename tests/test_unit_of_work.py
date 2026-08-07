"""Unit-of-work seam (architecture-deepening D0).

Two guarantees:

1. **emit-iff-commit** — a ``unit_of_work()`` scope that raises before
   commit fires ZERO side effects: no EventBus publish, no
   ``agent_actions`` audit row, no ``g.audit_log`` entry, no cache
   hook, and the DB write is rolled back. This is the invariant that
   makes the "forgot to notify" bug class (BL-R26-1) unrepresentable —
   you cannot emit without committing.

2. **delete_task migrated onto the uow** — the migrated
   ``delete_task_tool_impl`` still emits ``task.deleted``, evicts the
   cache, and writes its ``deleted_task`` audit (now through the uow's
   unified both-sinks ``audit``), on a clean commit.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# --- helpers ---------------------------------------------------------------


def _capture_publishes(monkeypatch) -> list:
    published: list = []

    def _capture(agent_id, event_type, payload=None):
        published.append((agent_id, event_type, payload))

    monkeypatch.setattr(
        "agent_mcp.core.event_bus_shim.publish", _capture
    )
    return published


def _seed_task(*, title: str = "seeded", assigned_to: str | None = None) -> str:
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (task_id, title, description, status, "
            "priority, assigned_to, created_by, created_at, updated_at, "
            "parent_task, child_tasks) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                "seeded description",
                "pending",
                "medium",
                assigned_to,
                "admin",
                now,
                now,
                None,
                json.dumps([]),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def _task_exists(task_id: str) -> bool:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _audit_rows(action_type: str, task_id: str) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_actions "
            "WHERE action_type = ? AND task_id = ?",
            (action_type, task_id),
        ).fetchone()
    finally:
        conn.close()
    return row["n"]


def _first_text(result) -> str:
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


# --- 1. emit-iff-commit: rollback fires ZERO effects ----------------------


async def test_uow_rollback_fires_zero_side_effects(tmp_path, monkeypatch):
    """A uow scope that raises before commit must:
    - roll back the DB write (the task row survives),
    - publish NOTHING,
    - write NO agent_actions audit row,
    - append NOTHING to g.audit_log,
    - run NO on_commit cache hook.

    This is the whole point of the seam: emit is impossible without
    commit, so "forgot to notify" cannot ship as a half-applied write.
    """
    async with mcp_session(tmp_path):
        from agent_mcp.core import globals as g
        from agent_mcp.db.unit_of_work import unit_of_work

        task_id = _seed_task(title="rollback-target", assigned_to="alice")

        published = _capture_publishes(monkeypatch)
        hook_fired: list = []
        audit_len_before = len(g.audit_log)

        class _Boom(Exception):
            pass

        with pytest.raises(_Boom), unit_of_work() as u:
            u.cursor.execute(
                "DELETE FROM tasks WHERE task_id = ?", (task_id,)
            )
            u.emit("alice", "task.deleted", {"task_id": task_id})
            u.audit(
                "admin",
                "deleted_task",
                task_id=task_id,
                details={"title": "rollback-target"},
            )
            u.on_commit(lambda: hook_fired.append("nope"))
            # Something blows up AFTER we registered effects but
            # BEFORE __exit__ commits.
            raise _Boom()

        # DB write rolled back — the row is still there.
        assert _task_exists(task_id), (
            "rolled-back uow must NOT delete the row"
        )
        # No EventBus publish fired.
        assert published == [], (
            f"rolled-back uow must publish nothing; saw {published}"
        )
        # No DB audit row.
        assert _audit_rows("deleted_task", task_id) == 0, (
            "rolled-back uow must write no agent_actions row"
        )
        # No in-memory audit entry.
        assert len(g.audit_log) == audit_len_before, (
            "rolled-back uow must append nothing to g.audit_log"
        )
        # No cache hook.
        assert hook_fired == [], (
            "rolled-back uow must not run on_commit hooks"
        )


async def test_uow_commit_flushes_effects_in_registration_order(
    tmp_path, monkeypatch
):
    """On a clean commit, emit + audit + on_commit hooks flush in the
    order they were registered."""
    async with mcp_session(tmp_path):
        from agent_mcp.db.unit_of_work import unit_of_work

        task_id = _seed_task(title="commit-target")
        _capture_publishes(monkeypatch)
        order: list = []

        with unit_of_work() as u:
            u.cursor.execute(
                "DELETE FROM tasks WHERE task_id = ?", (task_id,)
            )
            u.on_commit(lambda: order.append("a"))
            u.emit("*", "task.deleted", {"task_id": task_id})
            u.on_commit(lambda: order.append("b"))

        assert u.committed is True
        assert not _task_exists(task_id), "committed uow must delete the row"
        # a registered before emit before b -> a, (emit runs), b.
        assert order == ["a", "b"], f"hooks out of order: {order}"


# --- 2. delete_task migrated onto the uow ---------------------------------


async def test_delete_task_migrated_emits_evicts_and_audits(
    tmp_path, monkeypatch
):
    """The migrated delete_task path must, on a successful delete:
    - publish ``task.deleted`` for the task,
    - evict it from the g.tasks cache,
    - write a ``deleted_task`` audit row (DB sink), and
    - append a ``deleted_task`` entry to g.audit_log (in-memory sink).
    """
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        create = await admin.call(
            "assign_task",
            {"task_title": "delete me", "task_description": "d"},
        )
        import re

        m = re.search(r"task_[a-f0-9_]+", _first_text(create))
        assert m, f"no task id in create response: {_first_text(create)!r}"
        task_id = m.group(0)
        assert task_id in g.tasks, "sanity: created task should be cached"

        published = _capture_publishes(monkeypatch)
        audit_before = len(g.audit_log)

        res = await admin.call(
            "delete_task", {"task_id": task_id, "force_delete": True}
        )
        assert not getattr(admin, "_last_is_error", False), _first_text(res)

        # Event fired.
        deleted = [e for e in published if e[1] == "task.deleted"]
        assert any(e[2].get("task_id") == task_id for e in deleted), (
            f"delete must publish task.deleted for {task_id}; saw {published}"
        )
        # Cache evicted.
        assert task_id not in g.tasks, (
            "deleted task must be evicted from g.tasks"
        )
        # DB audit sink.
        assert _audit_rows("deleted_task", task_id) == 1, (
            "delete must write exactly one deleted_task agent_actions row"
        )
        # In-memory audit sink (both-sinks unification via u.audit).
        assert len(g.audit_log) > audit_before, (
            "delete must append a deleted_task entry to g.audit_log"
        )
        assert any(
            entry.get("action") == "deleted_task"
            for entry in g.audit_log[audit_before:]
        ), "g.audit_log must carry the deleted_task action"
