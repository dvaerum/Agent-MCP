"""Worker self-claim must NOT require the worker to supply its own token.

Reported live: a worker agent (a Claude Code instance) cannot see its own
bearer token — it's the transport credential in the MCP client config,
not reachable from the agent's reasoning context. So the documented
"self-claim = pass agent_token=<your own>" flow was impossible: the agent
has no way to produce its own token as an argument.

Fix: the caller is already authenticated via the bearer, and the
Principal carries it (source_token). A worker passing task_ids (existing
tasks) WITHOUT agent_token self-claims as the AUTHENTICATED caller —
no token echo. Security is unchanged: it can only self-assign (never to
another agent), and only UNASSIGNED tasks are claimable.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_task(task_id: str, title: str, *, assigned_to: str | None,
               status: str = "pending") -> None:
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
        "parent_task": None, "child_tasks": [], "depends_on_tasks": [],
        "notes": [],
    }


def _db_scalar(sql: str, *params):
    from agent_mcp.db.connection import get_db_connection
    conn = get_db_connection()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _text(blocks) -> str:
    return "\n".join(
        b.text for b in blocks if isinstance(getattr(b, "text", None), str)
    )


async def test_worker_self_claims_unassigned_without_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool = f"task_{secrets.token_hex(6)}"
        _seed_task(pool, "claimable", assigned_to=None)

        # NO agent_token — just task_ids. Must self-claim to alice.
        resp = _text(await alice.call("assign_task", {"task_ids": [pool]}))
        assignee = _db_scalar("SELECT assigned_to FROM tasks WHERE task_id=?", pool)
        assert assignee == "alice", (
            f"worker must self-claim without supplying a token; "
            f"assignee={assignee!r}, resp={resp}"
        )


async def test_worker_can_update_after_self_claim(tmp_path) -> None:
    """The whole point: claim, then be able to complete it."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool = f"task_{secrets.token_hex(6)}"
        _seed_task(pool, "claimable", assigned_to=None)

        await alice.call("assign_task", {"task_ids": [pool]})
        await alice.call(
            "update_task_status", {"task_id": pool, "status": "in_progress"}
        )
        status = _db_scalar("SELECT status FROM tasks WHERE task_id=?", pool)
        assert status == "in_progress", (
            f"after self-claim the worker must be able to update it; "
            f"status={status!r}"
        )


async def test_worker_cannot_self_claim_foreign_task(tmp_path) -> None:
    """SECURITY: the no-token self-claim must NOT let a worker grab a task
    assigned to ANOTHER worker — only unassigned tasks are claimable."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        bobs = f"task_{secrets.token_hex(6)}"
        _seed_task(bobs, "bob's task", assigned_to="bob")

        await alice.call("assign_task", {"task_ids": [bobs]})
        assignee = _db_scalar("SELECT assigned_to FROM tasks WHERE task_id=?", bobs)
        assert assignee == "bob", (
            f"a worker must NOT self-claim a foreign-owned task; "
            f"assignee={assignee!r}"
        )
