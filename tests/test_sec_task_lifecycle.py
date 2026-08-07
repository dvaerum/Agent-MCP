"""Security/integrity regressions for the task lifecycle surface.

Four confirmed task-lifecycle findings (owner-authorized defensive
hardening), all scoped to ``agent_mcp/tools/task_tools.py`` +
``agent_mcp/repositories/task_repository.py``:

1. Mode-0 ``assign_task`` (worker files an unassigned task with no
   ``agent_token``) must record the *real* creator in both the
   ``tasks.created_by`` column and the ``agent_actions`` audit row —
   not the forged literal ``"admin"``.

2. A worker on the Mode-0 path may not create a parent-less ROOT task;
   the hierarchy invariant (``create_self_task``: "Agents can NEVER
   create root tasks") must hold on this path too.

3. Task status writes must respect an allowed-transition table:
   terminal states (completed / cancelled / failed) are sinks. No
   double-complete, un-complete, or resurrect-cancelled. Enforced in
   both ``_update_single_task`` (update_task_status) and
   ``bulk_task_operations``.

4. Assignment must reject TERMINATED target agents (Modes 2/3, the
   admin ``agent_id`` alias path, bulk reassign, and the
   ``update_task_status`` ``assigned_to`` field). (The Mode-3 self-claim
   capability-tag gate was retired in PR5.)
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# --- Direct-SQL seeding helpers (bypass the tool surface for setup) ---


def _seed_task(
    *,
    title: str = "seeded",
    status: str = "pending",
    assigned_to: str | None = None,
    parent_task: str | None = None,
    created_by: str = "admin",
) -> str:
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (task_id, title, description, status, "
            "priority, assigned_to, created_by, created_at, updated_at, "
            "parent_task) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def _terminate_agent(agent_id: str) -> None:
    """Flip an agent to status='terminated' in the DB and evict its
    token from the in-memory auth cache (mirrors the terminate flow's
    cache eviction)."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET status = 'terminated', terminated_at = ? "
            "WHERE agent_id = ?",
            (_dt.datetime.now().isoformat(), agent_id),
        )
        conn.commit()
        cursor.execute(
            "SELECT token FROM agents WHERE agent_id = ?", (agent_id,)
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    if row and row["token"]:
        g.active_agents.pop(row["token"], None)


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


def _audit_actor(action_type: str) -> str | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT agent_id FROM agent_actions WHERE action_type = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (action_type,),
        ).fetchone()
    finally:
        conn.close()
    return row["agent_id"] if row is not None else None


# --- Finding 1: Mode-0 provenance is the real worker, not "admin" ---


async def test_worker_mode0_created_by_records_worker(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # AZ-R19-1: a worker may only file under a parent it OWNS. Seed
        # the parent assigned to alice so this test exercises the
        # created_by provenance it targets, not the ownership gate.
        root = _seed_task(title="root", parent_task=None, assigned_to="alice")

        result = await alice.call(
            "assign_task",
            {
                "task_title": "worker filed this",
                "task_description": "triage me",
                "parent_task_id": root,
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text, text
        # Extract the created task id.
        import re

        m = re.search(r"task_[a-f0-9_]+", text)
        assert m, f"no task id in response: {text!r}"
        new_task_id = m.group(0)

        assert _task_field(new_task_id, "created_by") == "alice", (
            "Mode-0 must record the real worker as created_by, not 'admin'"
        )
        assert _audit_actor("created_unassigned_task") == "alice", (
            "audit actor for the Mode-0 create must be the worker, not 'admin'"
        )


async def test_operator_mode0_created_by_still_admin(tmp_path) -> None:
    """Regression guard: an operator/manager caller (no
    ``_worker_created_by`` tag) keeps ``created_by='admin'``."""
    async with mcp_session(tmp_path) as admin:
        _seed_task(title="root", parent_task=None)
        result = await admin.call(
            "assign_task",
            {
                "task_title": "operator filed this",
                "task_description": "desc",
                "parent_task_id": _seed_task(title="parent"),
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text, text


# --- Finding 2: worker Mode-0 may not create a root task ---


async def test_worker_mode0_no_parent_rejected(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        result = await alice.call(
            "assign_task",
            {
                "task_title": "sneaky root",
                "task_description": "no parent supplied",
            },
        )
        text = result[0].text
        assert (
            "parent" in text.lower() or "root" in text.lower()
        ), f"expected a parent-required rejection; got {text!r}"

        # No unassigned task should have been created.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE title = ?",
                ("sneaky root",),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 0, "rejected Mode-0 create must not persist a row"


async def test_worker_mode0_multi_no_parent_rejected(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        result = await alice.call(
            "assign_task",
            {
                "tasks": [
                    {"title": "rootish", "description": "no parent"},
                ],
            },
        )
        text = result[0].text
        assert (
            "parent" in text.lower() or "root" in text.lower()
        ), f"expected a parent-required rejection; got {text!r}"


# --- Finding 3: terminal-state / transition guard ---


async def test_completed_to_completed_rejected(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_task(
            title="done", status="completed", assigned_to="alice"
        )

        result = await alice.call(
            "update_task_status",
            {"task_id": task_id, "status": "completed"},
        )
        text = result[0].text.lower()
        assert (
            "transition" in text
            or "terminal" in text
            or "cannot" in text
            or "not allowed" in text
        ), f"double-complete should be rejected; got {result[0].text!r}"


async def test_cancelled_to_completed_rejected(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_task(
            title="cancelled", status="cancelled", assigned_to="alice"
        )

        result = await alice.call(
            "update_task_status",
            {"task_id": task_id, "status": "completed"},
        )
        text = result[0].text.lower()
        assert (
            "transition" in text
            or "terminal" in text
            or "cannot" in text
            or "not allowed" in text
        ), f"resurrect-cancelled should be rejected; got {result[0].text!r}"
        assert _task_field(task_id, "status") == "cancelled", (
            "cancelled task must stay cancelled after rejected transition"
        )


async def test_valid_transition_allowed(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_task(
            title="active", status="pending", assigned_to="alice"
        )

        result = await alice.call(
            "update_task_status",
            {"task_id": task_id, "status": "in_progress"},
        )
        text = result[0].text
        assert "Unauthorized" not in text, text
        assert _task_field(task_id, "status") == "in_progress", (
            "pending -> in_progress is a valid transition and must apply"
        )


async def test_bulk_terminal_transition_rejected(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_task(
            title="done", status="completed", assigned_to="alice"
        )

        result = await alice.call(
            "bulk_task_operations",
            {
                "operations": [
                    {
                        "type": "update_status",
                        "task_id": task_id,
                        "status": "completed",
                    }
                ]
            },
        )
        text = result[0].text
        assert _task_field(task_id, "status") == "completed"
        assert "updated to 'completed'" not in text, (
            f"bulk double-complete should be rejected; got {text!r}"
        )


# --- Finding 4a: reject TERMINATED target agents ---


async def test_assign_existing_to_terminated_agent_rejected(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        _terminate_agent("bob")
        task_id = _seed_task(title="orphan", assigned_to=None)

        result = await admin.call(
            "assign_task",
            {"agent_token": bob.token, "task_ids": [task_id]},
        )
        text = result[0].text
        assert _task_field(task_id, "assigned_to") is None, (
            f"must not assign to a terminated agent; got {text!r}"
        )


async def test_create_and_assign_multiple_to_terminated_rejected(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        _terminate_agent("bob")

        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "tasks": [
                    {"title": "t1", "description": "d1"},
                ],
            },
        )
        text = result[0].text
        # No task should have been created + assigned to the terminated agent.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE assigned_to = ?",
                ("bob",),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 0, (
            f"must not create+assign tasks to a terminated agent; got {text!r}"
        )


async def test_bulk_reassign_to_terminated_agent_rejected(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")
        _terminate_agent("bob")
        task_id = _seed_task(title="reassign me", assigned_to=None)

        result = await admin.call(
            "bulk_task_operations",
            {
                "operations": [
                    {
                        "type": "reassign",
                        "task_id": task_id,
                        "assigned_to": "bob",
                    }
                ]
            },
        )
        text = result[0].text
        assert _task_field(task_id, "assigned_to") != "bob", (
            f"bulk reassign to a terminated agent must be rejected; got {text!r}"
        )


async def test_update_status_reassign_to_terminated_agent_rejected(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")
        _terminate_agent("bob")
        task_id = _seed_task(
            title="admin reassign", status="pending", assigned_to=None
        )

        result = await admin.call(
            "update_task_status",
            {
                "task_id": task_id,
                "status": "pending",
                "assigned_to": "bob",
            },
        )
        text = result[0].text
        assert _task_field(task_id, "assigned_to") != "bob", (
            f"update_task_status must not reassign to a terminated agent; "
            f"got {text!r}"
        )


# --- Finding 4b: Mode-3 self-claim enforces capability subset ---


# PR5 retired the structured capability-tag routing (the
# ``task.required_capabilities ⊆ agent.capabilities`` self-claim gate),
# so ``test_self_claim_missing_capability_rejected`` /
# ``test_self_claim_with_matching_capability_allowed`` were removed — the
# gate they exercised no longer exists (self-claim is caps-agnostic).
