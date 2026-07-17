"""create-unassigned root-task guidance names the OWNED-parent requirement.

A worker filing a task via assign_task (Mode-0) must specify a
parent_task_id. The denial told them to "specify a parent_task_id" but
NOT that the parent must be a task they OWN — so a worker naming a
foreign parent hit the AZ-R19-1 phantom-404 dead-end ("task not found")
and reported a false bug. Naming the owned-parent rule is safe: it's a
general rule about the parameter, revealing no specific task's existence
or owner (the phantom for a specific foreign parent is unchanged).
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(task_id: str, title: str, *, assigned_to, status="pending") -> None:
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, status, priority, "
            " assigned_to, created_by, created_at, updated_at, parent_task, "
            " child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, 'd', ?, 'medium', ?, 'admin', ?, ?, NULL, "
            "        '[]', '[]', '[]')",
            (task_id, title, status, assigned_to, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    g.tasks[task_id] = {
        "task_id": task_id, "title": title, "description": "d",
        "status": status, "priority": "medium", "assigned_to": assigned_to,
        "created_by": "admin", "created_at": now, "updated_at": now,
        "parent_task": None, "child_tasks": [], "depends_on_tasks": [], "notes": [],
    }


def _text(blocks) -> str:
    return "\n".join(
        b.text for b in blocks if isinstance(getattr(b, "text", None), str)
    )


async def test_worker_single_create_names_owned_parent(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        text = _text(await alice.call(
            "assign_task", {"task_title": "x", "task_description": "y"}
        )).lower()
        assert "parent_task_id" in text and "you own" in text, (
            f"root-task denial should name the owned-parent rule; got: {text}"
        )


async def test_worker_tasks_array_create_names_owned_parent(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        text = _text(await alice.call(
            "assign_task", {"tasks": [{"title": "x", "description": "y"}]}
        )).lower()
        assert "you own" in text, (
            f"tasks-array root denial should name the owned-parent rule; got: {text}"
        )


async def test_worker_foreign_parent_still_phantom(tmp_path) -> None:
    """SECURITY (AZ-R19-1): a FOREIGN parent must STILL phantom-404 — the
    owned-parent RULE is safe to state, but a SPECIFIC foreign parent's
    existence must never be confirmed."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        bobs = f"task_{secrets.token_hex(6)}"
        _seed_task(bobs, "bob's", assigned_to="bob")
        text = _text(await alice.call(
            "assign_task",
            {"task_title": "x", "task_description": "y", "parent_task_id": bobs},
        )).lower()
        assert "not found" in text, f"foreign parent must phantom; got: {text}"
        assert "bob" not in text, f"must not leak the parent owner; got: {text}"
