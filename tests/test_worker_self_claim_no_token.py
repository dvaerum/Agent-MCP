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


# ── own-task error clarity (assign Mode-3 must split self from foreign) ──
#
# The defect: Mode-3's "already assigned" / "terminal" checks tested
# ``assigned_to is not None`` / ``status in TERMINAL`` WITHOUT comparing to
# the claiming worker, so a worker re-issuing assign_task on its OWN task —
# a task plainly in its own view_tasks list — got the phantom "not found".
# The owner already knows the task exists, so hiding it is gratuitous +
# misleading. Fix: self-owned → informative; FOREIGN/nonexistent → the
# UNCHANGED phantom-404 (the AZ-R17/AZ-R18 existence oracle must hold).


async def test_worker_claiming_own_active_task_is_informative(tmp_path) -> None:
    async with mcp_session(tmp_path) as alice_admin:
        alice = await alice_admin.create_worker("alice")
        mine = f"task_{secrets.token_hex(6)}"
        _seed_task(mine, "already mine", assigned_to="alice", status="in_progress")

        text = _text(await alice.call("assign_task", {"task_ids": [mine]})).lower()
        assert "already assigned to you" in text, (
            f"claiming a task the worker already owns must say so, not phantom; "
            f"got: {text}"
        )
        assert "not found" not in text, (
            f"a self-owned task must NOT render the phantom 'not found'; got: {text}"
        )


async def test_worker_claiming_own_terminal_task_is_informative(tmp_path) -> None:
    async with mcp_session(tmp_path) as alice_admin:
        alice = await alice_admin.create_worker("alice")
        done = f"task_{secrets.token_hex(6)}"
        _seed_task(done, "my finished task", assigned_to="alice", status="completed")

        text = _text(await alice.call("assign_task", {"task_ids": [done]})).lower()
        assert "already assigned to you" in text, (
            f"claiming an own terminal task must name it as yours; got: {text}"
        )
        # names the terminal state so the worker understands why it can't reclaim
        assert "completed" in text or "terminal" in text, (
            f"own terminal task message should name the terminal state; got: {text}"
        )
        assert "not found" not in text, f"must not phantom an own task; got: {text}"


async def test_worker_claiming_foreign_task_still_phantom(tmp_path) -> None:
    """SECURITY regression: a FOREIGN-owned task must STILL collapse to the
    phantom 'not found' — never reveal it exists nor the owner's id."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        bobs = f"task_{secrets.token_hex(6)}"
        _seed_task(bobs, "bob's secret", assigned_to="bob", status="in_progress")

        text = _text(await alice.call("assign_task", {"task_ids": [bobs]})).lower()
        assert "not found" in text, (
            f"a foreign task must render the phantom 'not found'; got: {text}"
        )
        assert "bob" not in text, (
            f"the phantom must NOT leak the owning agent id; got: {text}"
        )
        assert "already assigned to you" not in text


async def test_worker_claiming_nonexistent_still_phantom(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        ghost = f"task_{secrets.token_hex(6)}"
        text = _text(await alice.call("assign_task", {"task_ids": [ghost]})).lower()
        assert "not found" in text, f"nonexistent task must phantom; got: {text}"
        assert "already assigned to you" not in text


async def test_worker_agent_id_denial_points_to_token_free_self_claim(tmp_path) -> None:
    """A worker passing agent_id (admin-only) must NOT be told to "pass
    agent_token (their own token)" — that's the exact false guidance behind
    the original bug (a worker cannot access its own token). It must be
    pointed at the token-free task_ids self-claim path."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool = f"task_{secrets.token_hex(6)}"
        _seed_task(pool, "claimable", assigned_to=None)
        text = _text(
            await alice.call(
                "assign_task", {"task_ids": [pool], "agent_id": "alice"}
            )
        ).lower()
        assert "task_ids" in text and "self-claim" in text, (
            f"denial should point to the token-free task_ids self-claim; got: {text}"
        )
        assert "must pass agent_token" not in text and "pass agent_token" not in text, (
            f"must NOT tell a worker to pass its own token; got: {text}"
        )


async def test_view_tasks_foreign_filter_gives_actionable_hint(tmp_path) -> None:
    """view_tasks(agent_id=<another agent>) is denied for a worker — the
    denial should point at the working path (omit the filter), not just say
    no. This is a blanket role check (fires regardless of whether the id
    names a real agent), so the hint is not an existence oracle."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        text = _text(await alice.call("view_tasks", {"agent_id": "bob"})).lower()
        assert "omit" in text and "agent_id" in text, (
            f"the denial should point to the omit-filter path; got: {text}"
        )
