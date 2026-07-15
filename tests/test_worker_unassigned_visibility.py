"""Workers must be able to SEE the unassigned task pool (the claimable
pool) so the documented self-claim loop actually closes.

Bug (reported live). The write side of self-claim works — a worker can
file a task with no ``agent_token`` (Mode 0, gated by
``config_allow_worker_create_unassigned``) and can self-claim an
existing unassigned task by id (Mode 3, gated by
``config_allow_worker_self_assign``). But the READ side never let a
worker DISCOVER an unassigned task it did not create:

- ``view_tasks`` filtered a non-admin caller to ``assigned_to == me``,
  so unassigned rows (``assigned_to IS NULL``) never appeared.
- ``search_tasks`` skipped every row where
  ``assigned_to != requesting_agent_id`` — which also excludes NULL.

So a worker could file into the pool and claim by id, but could never
find an id it didn't already know. Broken loop.

Fix: for a non-admin worker, task READ visibility is
**assigned to me OR unassigned (the claimable pool)**. Foreign-owned
tasks (``assigned_to == <other worker>``) stay hidden — the
cross-worker isolation invariant (AZ-R17-1 phantom-404 / PF-1) is
preserved.

Design note: pool VISIBILITY is unconditional for workers — it is NOT
gated on ``config_allow_worker_self_assign``. The unassigned pool is
shared work; the toggle only gates the write-side ACT of claiming.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_task(
    task_id: str,
    title: str,
    *,
    assigned_to: str | None,
    status: str = "pending",
    description: str = "task in the pool",
) -> None:
    """Populate BOTH the tasks table (so ``assign_task`` Mode 3, which
    reads the DB, can self-claim) and ``g.tasks`` (the in-memory cache
    the read tools ``view_tasks`` / ``search_tasks`` actually query)."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, title, description, status, priority, assigned_to, "
            " created_by, created_at, updated_at, parent_task, child_tasks, "
            " depends_on_tasks, notes) "
            "VALUES (?, ?, ?, ?, 'medium', ?, 'admin', ?, ?, NULL, "
            "        '[]', '[]', '[]')",
            (task_id, title, description, status, assigned_to, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": "medium",
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "parent_task": None,
        "child_tasks": [],
        "depends_on_tasks": [],
        "notes": [],
    }


def _text(blocks) -> str:
    return "\n".join(
        b.text for b in blocks if isinstance(getattr(b, "text", None), str)
    )


# --- RED #1: view_tasks surfaces the unassigned pool to a worker ------


async def test_worker_view_tasks_includes_unassigned(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool_id = f"task_{secrets.token_hex(6)}"
        _seed_task(pool_id, "unowned work", assigned_to=None)

        text = _text(await alice.call("view_tasks", {}))
        assert pool_id in text, (
            "worker view_tasks must include the unassigned (claimable) "
            f"pool; got: {text}"
        )


# --- RED #2: search_tasks (scored query) surfaces the pool ------------


async def test_worker_search_tasks_includes_unassigned(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool_id = f"task_{secrets.token_hex(6)}"
        _seed_task(
            pool_id,
            "refactor the widget parser",
            assigned_to=None,
            description="the widget parser needs a rewrite",
        )

        text = _text(
            await alice.call("search_tasks", {"search_query": "widget parser"})
        )
        assert pool_id in text, (
            "worker search_tasks (scored query) must return an unassigned "
            f"pool task; got: {text}"
        )


# --- RED #3: search_tasks filter-only discovery surfaces the pool -----
# (this codebase has no discrete `get_task` MCP tool; the filter-only
#  search path is the worker's "find this specific unassigned task"
#  discovery surface — a distinct code path from the scored search.)


async def test_worker_search_filter_only_includes_unassigned(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool_id = f"task_{secrets.token_hex(6)}"
        _seed_task(pool_id, "pending pool item", assigned_to=None)

        text = _text(
            await alice.call("search_tasks", {"status_filter": "pending"})
        )
        assert pool_id in text, (
            "worker filter-only search must return an unassigned pool "
            f"task; got: {text}"
        )


# --- SECURITY (must stay GREEN): foreign-owned tasks stay hidden ------


async def test_worker_cannot_see_other_workers_task_via_view(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        bobs_id = f"task_{secrets.token_hex(6)}"
        _seed_task(bobs_id, "bob's secret work", assigned_to="bob")

        text = _text(await alice.call("view_tasks", {}))
        assert bobs_id not in text, (
            "cross-worker isolation regressed: alice can see bob's task via "
            f"view_tasks; got: {text}"
        )


async def test_worker_cannot_see_other_workers_task_via_search(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        bobs_id = f"task_{secrets.token_hex(6)}"
        _seed_task(
            bobs_id,
            "bob confidential migration",
            assigned_to="bob",
            description="bob confidential migration details",
        )

        scored = _text(
            await alice.call(
                "search_tasks", {"search_query": "confidential migration"}
            )
        )
        assert bobs_id not in scored, (
            "cross-worker isolation regressed: alice found bob's task via "
            f"scored search; got: {scored}"
        )

        filtered = _text(
            await alice.call("search_tasks", {"status_filter": "pending"})
        )
        assert bobs_id not in filtered, (
            "cross-worker isolation regressed: alice found bob's task via "
            f"filter-only search; got: {filtered}"
        )


# --- END-TO-END: discover -> self-claim -> now owned ------------------


async def test_worker_discovers_then_self_claims_pool_task(tmp_path) -> None:
    """Full loop: worker sees an unassigned task via view_tasks, then
    self-claims it (Mode 3, toggle default-on), and afterwards it shows
    as assigned to them and no longer as a foreign row to a peer."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        pool_id = f"task_{secrets.token_hex(6)}"
        _seed_task(pool_id, "claim me", assigned_to=None)

        # 1. discover
        discovered = _text(await alice.call("view_tasks", {}))
        assert pool_id in discovered, discovered

        # 2. self-claim (Mode 3)
        claim = _text(
            await alice.call(
                "assign_task",
                {"agent_token": alice.token, "task_ids": [pool_id]},
            )
        )
        assert "Unauthorized" not in claim, claim

        # 3. authoritative check: DB now shows alice as owner
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id = ?", (pool_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row["assigned_to"] == alice.agent_id, (
            f"self-claim did not land; assigned_to={row and row['assigned_to']!r}"
        )

        # 4. still visible to alice, now as her own task
        after = _text(await alice.call("view_tasks", {}))
        assert pool_id in after, after
