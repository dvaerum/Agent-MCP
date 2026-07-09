"""Round-3 finding BL-3 — ``force_delete`` must clear ``agents.current_task``.

PR #301 (BL-2) made ``delete_task_tool_impl``'s force-cascade enumerate
descendants authoritatively from the ``tasks.parent_task`` self-FK, so a
parent with children could finally be force-deleted. A SEPARATE FK
remained: ``agents.current_task → tasks.task_id``. If any task in the
delete set (the target OR a cascaded descendant) is referenced by some
agent's ``current_task`` column, the ``DELETE FROM tasks`` still raised
``FOREIGN KEY constraint failed`` — so ``force_delete=True`` again did NOT
actually force.

Fix: before deleting the task set on the ``force_delete`` path, NULL
``agents.current_task`` for every agent whose pointer is in the set (a
single ``UPDATE ... WHERE current_task IN (...)`` in the same
transaction). Routed through ``agent_repo.clear_current_task_for_many`` so
the in-memory agent cache mirror stays consistent.

Scope guard preserved: a *non-force* delete of an in-use task is still
refused (now with a clear message rather than a raw FK crash).
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# --- helpers ---------------------------------------------------------------


def _seed_task(
    *,
    title: str = "seeded",
    status: str = "pending",
    assigned_to: str | None = None,
    parent_task: str | None = None,
    created_by: str = "admin",
) -> str:
    """Insert a task row directly (bypassing the tool surface) for setup."""
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
                status,
                "medium",
                assigned_to,
                created_by,
                now,
                now,
                parent_task,
                json.dumps([]),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def _set_current_task(agent_id: str, task_id: str | None) -> None:
    """Point ``agent_id.current_task`` at ``task_id`` (raw SQL setup)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE agents SET current_task = ? WHERE agent_id = ?",
            (task_id, agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def _task_field(task_id: str, field: str):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            f"SELECT {field} FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[field] if row is not None else None


def _agent_current_task(agent_id: str):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT current_task FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["current_task"] if row is not None else None


def _first_text(result) -> str:
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


# --- BL-3: force_delete clears the target's current_task ------------------


async def test_force_delete_clears_agent_current_task(tmp_path) -> None:
    """A task that IS an agent's ``current_task`` must be force-deletable:
    the pointer is NULLed in the same transaction, the DELETE succeeds, and
    the agent's ``current_task`` ends up NULL. RED against origin/main
    (FOREIGN KEY constraint failed)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_task(title="alice work", assigned_to=alice.agent_id)
        _set_current_task("alice", task_id)
        assert _agent_current_task("alice") == task_id, "sanity: pointer set"

        res = await admin.call(
            "delete_task", {"task_id": task_id, "force_delete": True}
        )
        text = _first_text(res)
        assert not getattr(admin, "_last_is_error", False), (
            f"force_delete of an in-use task must succeed; got {text!r}"
        )
        assert "FOREIGN KEY" not in text, text

        assert _task_field(task_id, "task_id") is None, "task must be deleted"
        assert _agent_current_task("alice") is None, (
            "agent's current_task must be NULLed by the force delete"
        )


# --- BL-3: force_delete clears a cascaded descendant's current_task -------


async def test_force_delete_clears_descendant_current_task(tmp_path) -> None:
    """When a cascaded *descendant* (not the target) is an agent's
    ``current_task``, the force-cascade must NULL that pointer too so the
    subtree DELETE doesn't trip the FK."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        parent_id = _seed_task(title="parent", parent_task=None)
        child_id = _seed_task(
            title="child", parent_task=parent_id, assigned_to=bob.agent_id
        )
        _set_current_task("bob", child_id)
        assert _agent_current_task("bob") == child_id, "sanity: pointer set"

        res = await admin.call(
            "delete_task", {"task_id": parent_id, "force_delete": True}
        )
        text = _first_text(res)
        assert not getattr(admin, "_last_is_error", False), (
            f"force_delete of parent must cascade the in-use child; "
            f"got {text!r}"
        )
        assert "FOREIGN KEY" not in text, text

        assert _task_field(parent_id, "task_id") is None, "parent deleted"
        assert _task_field(child_id, "task_id") is None, (
            "in-use child must be cascade-deleted, not orphaned"
        )
        assert _agent_current_task("bob") is None, (
            "descendant assignee's current_task must be NULLed"
        )


# --- regression: non-force delete of an in-use task is still refused ------


async def test_non_force_delete_in_use_task_refused(tmp_path) -> None:
    """A NON-force delete of a task that is an agent's ``current_task`` must
    be refused (task and pointer left intact) — preserving non-force
    semantics, now with a clear message instead of a raw FK crash."""
    async with mcp_session(tmp_path) as admin:
        carol = await admin.create_worker("carol")
        task_id = _seed_task(title="carol work", assigned_to=carol.agent_id)
        _set_current_task("carol", task_id)

        res = await admin.call(
            "delete_task", {"task_id": task_id, "force_delete": False}
        )
        text = _first_text(res)

        assert _task_field(task_id, "task_id") is not None, (
            "non-force delete of an in-use task must NOT delete it"
        )
        assert _agent_current_task("carol") == task_id, (
            "non-force delete must leave the agent's current_task intact"
        )
        assert "force_delete" in text, (
            f"refusal should point the caller at force_delete; got {text!r}"
        )


# --- regression: normal delete of a not-in-use task is unaffected ---------


async def test_delete_not_in_use_task_unaffected(tmp_path) -> None:
    """A plain delete of a task no agent points at must still succeed on
    the non-force path (no current_task check should block it)."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_task(title="free-standing", parent_task=None)

        res = await admin.call(
            "delete_task", {"task_id": task_id, "force_delete": False}
        )
        text = _first_text(res)
        assert not getattr(admin, "_last_is_error", False), (
            f"plain delete of a not-in-use task must succeed; got {text!r}"
        )
        assert _task_field(task_id, "task_id") is None, "task must be deleted"
