"""Unit-of-work migration of task_tools mutations (architecture-deepening D1).

D0 (PR #400) introduced the ``unit_of_work()`` seam and migrated
``delete_task`` as the proof. D1 migrates the remaining task_tools
mutations — ``assign_task`` (Mode 1/2/3), ``create_self_task``,
``update_task_status`` and ``bulk_task_operations`` — onto the same
seam, so *emit-iff-commit* holds for every write path: the post-commit
side effects (assignee wake, cache upsert, audit) fire only after a
successful commit, and a rollback fires NOTHING.

These tests mirror ``test_unit_of_work.py`` but drive the real tool
surface (through ``mcp_session``) rather than the raw seam:

1. **commit fires the effects** — a successful ``assign_task`` /
   ``update_task_status`` wakes the assignee, writes the DB audit row,
   and reflects the write in the ``g.tasks`` cache.
2. **rollback fires ZERO effects** — when a write inside the migrated
   ``assign_task`` scope raises before commit, the task row is NOT
   created, the assignee is NOT woken, NO audit row is written, and the
   cache stays clean. This is the D1 invariant that makes the
   "forgot to notify" class (BL-R26-1) unrepresentable on these paths.
"""

from __future__ import annotations

import re

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# --- helpers ---------------------------------------------------------------


def _first_text(result) -> str:
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


def _task_id_from(result) -> str:
    m = re.search(r"task_[a-f0-9_]+", _first_text(result))
    assert m, f"no task id in response: {_first_text(result)!r}"
    return m.group(0)


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


def _audit_rows_by_action(action_type: str) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_actions WHERE action_type = ?",
            (action_type,),
        ).fetchone()
    finally:
        conn.close()
    return row["n"]


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


def _capture_inbox_wakes(monkeypatch) -> list:
    """Record every ``notify_agent_inbox`` fan-out (the assignee wake)."""
    woken: list = []

    def _capture(agent_id):
        woken.append(agent_id)

    monkeypatch.setattr(
        "agent_mcp.core.globals.notify_agent_inbox", _capture
    )
    return woken


# --- 1. commit fires the effects ------------------------------------------


async def test_assign_task_commit_fires_wake_audit_cache(
    tmp_path, monkeypatch
):
    """A successful Mode-1 ``assign_task`` (create + assign) must, on
    commit, wake the assignee, write the ``assigned_task`` DB audit row,
    and cache the task in ``g.tasks``."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        alice = await admin.create_worker("alice")

        woken = _capture_inbox_wakes(monkeypatch)
        audit_before = len(g.audit_log)

        res = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "wake me",
                "task_description": "on commit",
            },
        )
        assert not getattr(admin, "_last_is_error", False), _first_text(res)
        task_id = _task_id_from(res)

        # Assignee woken post-commit.
        assert "alice" in woken, (
            f"assign must wake the assignee on commit; woke {woken}"
        )
        # DB audit sink.
        assert _audit_rows("assigned_task", task_id) == 1, (
            "assign must write exactly one assigned_task agent_actions row"
        )
        # In-memory audit sink.
        assert len(g.audit_log) > audit_before, (
            "assign must append an audit entry on commit"
        )
        # Cache reflects the write.
        assert task_id in g.tasks, "assigned task must be cached in g.tasks"
        assert g.tasks[task_id].get("assigned_to") == "alice"


async def test_update_task_status_commit_fires_wake_audit_cache(
    tmp_path, monkeypatch
):
    """A successful ``update_task_status`` must, on commit, wake the
    task's assignee, write the ``update_task_status`` DB audit row, and
    reflect the new status in ``g.tasks``."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        alice = await admin.create_worker("alice")
        create = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "status me",
                "task_description": "d",
            },
        )
        task_id = _task_id_from(create)

        woken = _capture_inbox_wakes(monkeypatch)
        audit_before = len(g.audit_log)

        res = await admin.call(
            "update_task_status",
            {"task_id": task_id, "status": "in_progress"},
        )
        assert not getattr(admin, "_last_is_error", False), _first_text(res)

        assert "alice" in woken, (
            f"status update must wake the assignee; woke {woken}"
        )
        assert _audit_rows("update_task_status", task_id) == 1, (
            "status update must write one update_task_status audit row"
        )
        assert len(g.audit_log) > audit_before, (
            "status update must append the aggregate audit entry on commit"
        )
        assert g.tasks[task_id].get("status") == "in_progress", (
            "cache must reflect the committed status"
        )


# --- 2. rollback fires ZERO effects ---------------------------------------


async def test_assign_task_rollback_fires_zero_side_effects(
    tmp_path, monkeypatch
):
    """When a write inside the migrated ``assign_task`` unit-of-work
    raises before commit, the seam must roll back and fire NOTHING:

    - the task row is NOT created,
    - the assignee is NOT woken,
    - NO ``assigned_task`` audit row is written,
    - NOTHING is appended to ``g.audit_log``,
    - the task is NOT cached in ``g.tasks``.

    This is the D1 emit-iff-commit invariant on the assign path: a
    half-applied write cannot leak side effects.
    """
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g
        from agent_mcp.tools import task_tools

        alice = await admin.create_worker("alice")

        woken = _capture_inbox_wakes(monkeypatch)
        audit_before = len(g.audit_log)
        tasks_before = set(g.tasks.keys())

        # Blow up AFTER task_repo.create writes the row (still inside the
        # uow scope) but BEFORE the scope commits — _link_child_to_parent
        # runs on every create path immediately after the INSERT.
        class _Boom(Exception):
            pass

        def _boom(*args, **kwargs):
            raise _Boom("induced mid-scope failure")

        monkeypatch.setattr(task_tools, "_link_child_to_parent", _boom)

        res = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "never lands",
                "task_description": "rolled back",
            },
        )
        # The tool swallows the exception into a Failed result.
        assert getattr(admin, "_last_is_error", False) or (
            "error" in _first_text(res).lower()
        ), f"expected a failure result, got {_first_text(res)!r}"

        # No new task row anywhere.
        new_task_ids = set(g.tasks.keys()) - tasks_before
        assert not new_task_ids, (
            f"rolled-back assign must not cache a task; leaked {new_task_ids}"
        )
        # No assignee wake.
        assert "alice" not in woken, (
            f"rolled-back assign must not wake the assignee; woke {woken}"
        )
        # No DB audit row landed — the assign never committed.
        assert _audit_rows_by_action("assigned_task") == 0, (
            "rolled-back assign must write no assigned_task agent_actions row"
        )
        # In-memory audit unchanged.
        assert len(g.audit_log) == audit_before, (
            "rolled-back assign must append nothing to g.audit_log"
        )


# --- 3. create-unassigned (bulk) path (D-R3-4: retired write_queue) --------


def _capture_unassigned_wakes(monkeypatch) -> list:
    """Record every ``notify_unassigned_task_appeared`` fan-out."""
    seen: list = []

    def _cap(task_id):
        seen.append(task_id)

    monkeypatch.setattr(
        "agent_mcp.core.globals.notify_unassigned_task_appeared", _cap
    )
    return seen


def _seed_assigned_parent(title: str, owner: str) -> str:
    """Insert a parent task owned by ``owner`` (AZ-R19-1 lets that owner
    file children / request assistance under it)."""
    import datetime as _dt
    import secrets

    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, parent_task) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, title, "d", "in_progress", "medium", owner, "admin",
         now, now, None),
    )
    conn.commit()
    conn.close()
    return task_id


async def test_create_unassigned_commit_fires_audit_cache_notify(
    tmp_path, monkeypatch
):
    """A successful Mode-0 ``assign_task`` (create unassigned) must, on
    commit, write the ``created_unassigned_task`` DB audit row, cache the
    task, and fan out the ``unassigned_task_appeared`` wake — replicating
    the retired ``write_queue`` path exactly. D3: this path writes ONLY
    the DB ``agent_actions`` sink, NOT the in-memory ``g.audit_log``."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        notified = _capture_unassigned_wakes(monkeypatch)
        audit_before = len(g.audit_log)

        res = await admin.call(
            "assign_task",
            {"task_title": "pool it", "task_description": "for the pool"},
        )
        assert not getattr(admin, "_last_is_error", False), _first_text(res)
        task_id = _task_id_from(res)

        # DB-only audit sink.
        assert _audit_rows("created_unassigned_task", task_id) == 1, (
            "unassigned create must write one created_unassigned_task row"
        )
        # D3: this path never touches the in-memory sink.
        assert len(g.audit_log) == audit_before, (
            "unassigned create writes only the DB sink, not g.audit_log"
        )
        # Cache reflects the committed write.
        assert task_id in g.tasks, "unassigned task must be cached in g.tasks"
        assert g.tasks[task_id].get("status") == "unassigned"
        # Post-commit notify fanout fired for the new task.
        assert any(t == task_id for t in notified), (
            f"unassigned create must wake the pool for {task_id}; "
            f"saw {notified}"
        )


async def test_create_unassigned_rollback_fires_zero_side_effects(
    tmp_path, monkeypatch
):
    """When a write inside the migrated create-unassigned unit-of-work
    raises before commit, the seam must roll back and fire NOTHING: no
    task cached, no ``created_unassigned_task`` audit row, no pool wake.
    This is the D-R3-4 emit-iff-commit invariant that replaces the
    retired ``write_queue`` (which had no such guarantee)."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g
        from agent_mcp.tools import task_tools

        notified = _capture_unassigned_wakes(monkeypatch)
        tasks_before = set(g.tasks.keys())
        audit_before = _audit_rows_by_action("created_unassigned_task")

        # Blow up AFTER task_repo.create writes the row but BEFORE commit —
        # _link_child_to_parent runs immediately after the INSERT.
        class _Boom(Exception):
            pass

        def _boom(*args, **kwargs):
            raise _Boom("induced mid-scope failure")

        monkeypatch.setattr(task_tools, "_link_child_to_parent", _boom)

        res = await admin.call(
            "assign_task",
            {"task_title": "never lands", "task_description": "rolled back"},
        )
        assert getattr(admin, "_last_is_error", False) or (
            "error" in _first_text(res).lower()
        ), f"expected a failure result, got {_first_text(res)!r}"

        # No new task cached.
        assert set(g.tasks.keys()) == tasks_before, (
            "rolled-back create must not cache a task"
        )
        # No DB audit row landed.
        assert _audit_rows_by_action("created_unassigned_task") == audit_before, (
            "rolled-back create must write no created_unassigned_task row"
        )
        # No pool wake.
        assert notified == [], (
            f"rolled-back create must not wake the pool; woke {notified}"
        )


# --- 4. request_assistance path (D-R3-4: retired atomic_with_audit) --------


async def test_request_assistance_commit_fires_db_and_memory_audit(
    tmp_path,
):
    """A successful ``request_assistance`` must, on commit, write exactly
    one ``request_assistance`` DB audit row (replicating the retired
    ``atomic_with_audit`` seam) AND — separately — append the in-memory
    ``log_audit`` entry. D3: the two sinks carry DIFFERENT details, so
    both must land (they are NOT folded)."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core import globals as g

        alice = await admin.create_worker("alice")
        parent_id = _seed_assigned_parent("alice parent", alice.agent_id)

        audit_before = len(g.audit_log)

        res = await alice.call(
            "request_assistance",
            {"task_id": parent_id, "description": "need help here"},
        )
        assert "Assistance requested" in _first_text(res), _first_text(res)

        # DB sink — exactly one request_assistance row for the parent.
        assert _audit_rows("request_assistance", parent_id) == 1, (
            "request_assistance must write one DB audit row on the parent"
        )
        # In-memory sink — the separate log_audit landed too.
        assert len(g.audit_log) > audit_before, (
            "request_assistance must also append the in-memory audit entry"
        )
        # The child assistance task committed + cached.
        child_ids = [
            tid for tid, t in g.tasks.items()
            if t.get("parent_task") == parent_id
        ]
        assert child_ids, "a child assistance task must be created + cached"


async def test_request_assistance_rollback_fires_zero_side_effects(
    tmp_path, monkeypatch
):
    """When the write inside the migrated ``request_assistance`` unit-of-
    work raises before commit, the seam must roll back and fire NOTHING:
    no child task committed, and no ``request_assistance`` DB audit row.
    Emit-iff-commit replaces the retired ``atomic_with_audit`` seam."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.tools import task_tools

        alice = await admin.create_worker("alice")
        parent_id = _seed_assigned_parent("alice parent", alice.agent_id)

        audit_before = _audit_rows_by_action("request_assistance")

        # Blow up on the LAST write inside the scope (the DB audit insert),
        # after the child INSERT + parent UPDATE but before commit.
        def _boom(*args, **kwargs):
            raise RuntimeError("induced mid-scope failure")

        monkeypatch.setattr(task_tools, "log_agent_action_to_db", _boom)

        res = await alice.call(
            "request_assistance",
            {"task_id": parent_id, "description": "never lands"},
        )
        assert "Assistance requested" not in _first_text(res), (
            f"expected a failure result, got {_first_text(res)!r}"
        )

        # The child assistance INSERT rolled back — no child row in the DB.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            child_rows = conn.execute(
                "SELECT task_id FROM tasks WHERE parent_task = ?",
                (parent_id,),
            ).fetchall()
        finally:
            conn.close()
        assert child_rows == [], (
            "rolled-back request_assistance must not commit a child task"
        )
        # No DB audit row landed.
        assert _audit_rows_by_action("request_assistance") == audit_before, (
            "rolled-back request_assistance must write no DB audit row"
        )
