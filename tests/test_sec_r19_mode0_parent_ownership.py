"""SEC-R19 — AZ-R19-1: worker Mode-0 create must not attach a child
under a FOREIGN parent (cross-agent stored-injection).

AZ-R19-1 (LOW-MED). In ``assign_task`` Mode 0 (a worker files an
unassigned task) the round-1 single-root guard forces the worker to
supply a ``parent_task_id``, but the create path +
``_link_child_to_parent`` never checked that the requesting WORKER may
attach to that parent. A worker could therefore attach an
attacker-titled child under ANY foreign / operator-owned parent,
mutating the victim parent's ``child_tasks`` JSON mirror — a
cross-agent stored-injection primitive (the victim sees an unexpected
child appear under their task).

Fix mirrors the ownership gate ``add_task_note`` /
``request_assistance`` enforce: a non-admin worker may only parent
under a task it OWNS (``assigned_to == worker``). A FOREIGN *or*
NONEXISTENT parent collapses to the SAME phantom ``NotFound`` (no
existence oracle — the worker can't tell "not yours" from "doesn't
exist"). Admins/managers keep the ability to parent under any task.
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
    assigned_to: str | None = None,
    child_tasks: list | None = None,
) -> str:
    """Insert a task row directly. Returns the task_id."""
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, child_tasks) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            "test description",
            "pending",
            "medium",
            assigned_to,
            "admin",
            now,
            now,
            json.dumps(child_tasks or []),
        ),
    )
    conn.commit()
    conn.close()
    return task_id


def _child_tasks(task_id: str) -> list:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT child_tasks FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["child_tasks"] or "[]") if row else []


# ── AZ-R19-1: cross-agent injection via foreign parent ───────────


async def test_worker_cannot_attach_child_under_foreign_parent(
    tmp_path,
) -> None:
    """A worker filing a Mode-0 task under a parent owned by ANOTHER
    agent must get a phantom NotFound AND leave the victim parent's
    child_tasks mirror UNCHANGED (no injected child)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        # A task owned by bob. Alice must not be able to inject a child.
        bob_parent = _seed_task(title="bob's task", assigned_to=bob.agent_id)

        res = await alice.call(
            "assign_task",
            {
                "task_title": "INJECTED malicious child",
                "task_description": "attacker-controlled body",
                "parent_task_id": bob_parent,
            },
        )
        text = res[0].text

        assert "not found" in text.lower(), (
            "worker filing under a foreign parent must get a phantom "
            f"NotFound; got {text!r}"
        )
        assert "Unauthorized" not in text, (
            "must not leak via PermissionDenied (existence oracle); "
            f"got {text!r}"
        )
        # The load-bearing assertion: the victim parent's mirror is
        # untouched — no injected child appeared under bob's task.
        assert _child_tasks(bob_parent) == [], (
            "worker must NOT be able to inject a child under a foreign "
            f"parent; bob's child_tasks was mutated: {_child_tasks(bob_parent)!r}"
        )


async def test_worker_foreign_and_nonexistent_parent_indistinguishable(
    tmp_path,
) -> None:
    """A foreign EXISTING parent and a NONEXISTENT parent must render
    IDENTICALLY (after masking the id) — no existence oracle."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        foreign_parent = _seed_task(title="bob's task", assigned_to=bob.agent_id)
        nonexistent_parent = "task_deadbeefdeadbeef"

        foreign_res = await alice.call(
            "assign_task",
            {
                "task_title": "child A",
                "task_description": "body",
                "parent_task_id": foreign_parent,
            },
        )
        nonexistent_res = await alice.call(
            "assign_task",
            {
                "task_title": "child A",
                "task_description": "body",
                "parent_task_id": nonexistent_parent,
            },
        )

        masked_foreign = foreign_res[0].text.replace(foreign_parent, "<T>")
        masked_nonexistent = nonexistent_res[0].text.replace(
            nonexistent_parent, "<T>"
        )
        assert masked_foreign == masked_nonexistent, (
            "foreign-existing and nonexistent parent responses must be "
            f"identical after masking; got {masked_foreign!r} vs "
            f"{masked_nonexistent!r}"
        )


# ── Class-sweep sibling: create_self_task parent ownership ───────


async def _approved_validator(*args, **kwargs):
    """Deterministic RAG stub: always approve placement so the test
    exercises the ownership gate, not the RAG denial path."""
    return {"status": "approved", "suggestions": {}, "message": ""}


async def test_worker_self_task_cannot_attach_under_foreign_parent(
    tmp_path, monkeypatch
) -> None:
    """CLASS-SWEEP: ``create_self_task`` is a worker-reachable write that
    links a worker-supplied parent_task_id — it must enforce the SAME
    ownership gate as Mode-0. A worker self-tasking under a FOREIGN
    parent must get a phantom NotFound and leave the victim's child_tasks
    UNCHANGED (no injected child)."""
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.validate_task_placement",
        _approved_validator,
    )
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        bob_parent = _seed_task(title="bob's task", assigned_to=bob.agent_id)

        res = await alice.call(
            "create_self_task",
            {
                "task_title": "INJECTED self-task",
                "task_description": "attacker-controlled body",
                "parent_task_id": bob_parent,
            },
        )
        text = res[0].text
        assert "not found" in text.lower(), (
            "worker self-tasking under a foreign parent must get a phantom "
            f"NotFound; got {text!r}"
        )
        assert _child_tasks(bob_parent) == [], (
            "worker must NOT inject a self-task under a foreign parent; "
            f"bob's child_tasks was mutated: {_child_tasks(bob_parent)!r}"
        )


async def test_worker_self_task_under_own_parent_succeeds(
    tmp_path, monkeypatch
) -> None:
    """Regression: a worker CAN self-task under a parent it OWNS."""
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.validate_task_placement",
        _approved_validator,
    )
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        own_parent = _seed_task(title="alice's task", assigned_to=alice.agent_id)

        res = await alice.call(
            "create_self_task",
            {
                "task_title": "legit self subtask",
                "task_description": "breaking down my own work",
                "parent_task_id": own_parent,
            },
        )
        text = res[0].text
        assert "not found" not in text.lower() and "Unauthorized" not in text, (
            f"worker must be able to self-task under its own parent; got {text!r}"
        )
        assert len(_child_tasks(own_parent)) == 1, (
            "self-task child should be mirrored onto the owned parent; "
            f"got {_child_tasks(own_parent)!r}"
        )


# ── Regressions ──────────────────────────────────────────────────


async def test_worker_can_attach_child_under_own_parent(tmp_path) -> None:
    """Regression: a worker CAN file a Mode-0 task under a parent it
    OWNS, and the child is mirrored onto the parent."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        own_parent = _seed_task(title="alice's task", assigned_to=alice.agent_id)

        res = await alice.call(
            "assign_task",
            {
                "task_title": "legit subtask",
                "task_description": "breaking down my own work",
                "parent_task_id": own_parent,
            },
        )
        text = res[0].text
        assert "not found" not in text.lower() and "Unauthorized" not in text, (
            f"worker must be able to parent under its own task; got {text!r}"
        )
        assert "Created" in text, text
        # The child was mirrored onto alice's own parent.
        assert len(_child_tasks(own_parent)) == 1, (
            "child should be mirrored onto the owned parent; "
            f"got {_child_tasks(own_parent)!r}"
        )


async def test_admin_can_attach_child_under_any_parent(tmp_path) -> None:
    """Regression: an admin/operator can file a Mode-0 task under any
    existing parent (not just its own)."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        bob_parent = _seed_task(title="bob's task", assigned_to=bob.agent_id)

        res = await admin.call(
            "assign_task",
            {
                "task_title": "operator-filed subtask",
                "task_description": "coordination breakdown",
                "parent_task_id": bob_parent,
            },
        )
        text = res[0].text
        assert "not found" not in text.lower() and "Unauthorized" not in text, (
            f"admin must be able to parent under any task; got {text!r}"
        )
        assert "Created" in text, text
        assert len(_child_tasks(bob_parent)) == 1, (
            "admin-filed child should be mirrored onto the parent; "
            f"got {_child_tasks(bob_parent)!r}"
        )
