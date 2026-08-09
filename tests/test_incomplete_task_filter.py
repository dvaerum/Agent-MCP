"""``incomplete`` status filter for view_tasks / search_tasks.

Both task-read tools filter by a *single* status (``view_tasks`` via
``status``; ``search_tasks`` via ``status_filter``). To list all open /
claimable work a caller previously had to query ``pending`` and
``in_progress`` separately. This adds an ``incomplete`` pseudo-value
(aliases ``active`` / ``open``) that expands to "any non-terminal
status" (:data:`agent_mcp.features.task_queries._ACTIVE_STATUSES` =
``{pending, in_progress}``), so one query returns the whole open set —
the natural companion to the worker unassigned-pool visibility fix
(a worker finding claimable work wants "unassigned AND incomplete").

Terminal statuses (``completed`` / ``cancelled`` / ``failed``) must be
excluded; a concrete status filter must still match exactly (no
regression).
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
    status: str = "pending",
    created_by: str = "admin",
    assigned_to: str | None = None,
) -> None:
    """Populate the tasks table + the ``g.tasks`` cache the read tools
    query. Defaults to an unassigned admin-created task so an admin
    listing sees it."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection
    from tests.conftest import ensure_seed_root

    # R15-BL-1: chain every seed under a dedicated hidden root so the
    # single-root invariant holds without making any asserted task id a
    # parent (view_tasks echoes parent ids in its output).
    parent = ensure_seed_root()

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, title, description, status, priority, assigned_to, "
            " created_by, created_at, updated_at, parent_task, child_tasks, "
            " depends_on_tasks, notes) "
            "VALUES (?, ?, 'desc', ?, 'medium', ?, ?, ?, ?, ?, "
            "        '[]', '[]', '[]')",
            (task_id, title, status, assigned_to, created_by, now, now,
             parent),
        )
        conn.commit()
    finally:
        conn.close()

    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "description": "desc",
        "status": status,
        "priority": "medium",
        "assigned_to": assigned_to,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "parent_task": parent,
        "child_tasks": [],
        "depends_on_tasks": [],
        "notes": [],
    }


def _text(blocks) -> str:
    return "\n".join(
        b.text for b in blocks if isinstance(getattr(b, "text", None), str)
    )


async def _seed_trio(admin):
    """One task in each of pending / in_progress / completed. Returns the
    three ids."""
    pend = f"task_{secrets.token_hex(6)}"
    prog = f"task_{secrets.token_hex(6)}"
    done = f"task_{secrets.token_hex(6)}"
    _seed_task(pend, "pending item", status="pending")
    _seed_task(prog, "in progress item", status="in_progress")
    _seed_task(done, "completed item", status="completed")
    return pend, prog, done


# ── view_tasks ───────────────────────────────────────────────────────


@pytest.mark.parametrize("alias", ["incomplete", "active", "open"])
async def test_view_tasks_incomplete_returns_open_excludes_terminal(
    tmp_path, alias: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        pend, prog, done = await _seed_trio(admin)
        text = _text(await admin.call("view_tasks", {"status": alias}))
        assert pend in text, f"{alias}: pending task must appear; got {text}"
        assert prog in text, f"{alias}: in_progress task must appear; got {text}"
        assert done not in text, (
            f"{alias}: completed (terminal) task must be excluded; got {text}"
        )


async def test_view_tasks_concrete_status_still_exact(tmp_path) -> None:
    """No regression: a concrete status filters to exactly that status."""
    async with mcp_session(tmp_path) as admin:
        pend, prog, done = await _seed_trio(admin)
        text = _text(await admin.call("view_tasks", {"status": "pending"}))
        assert pend in text
        assert prog not in text, "in_progress must NOT match status=pending"
        assert done not in text


# ── search_tasks (filter-only listing) ───────────────────────────────


@pytest.mark.parametrize("alias", ["incomplete", "active", "open"])
async def test_search_tasks_incomplete_returns_open_excludes_terminal(
    tmp_path, alias: str,
) -> None:
    async with mcp_session(tmp_path) as admin:
        pend, prog, done = await _seed_trio(admin)
        text = _text(
            await admin.call("search_tasks", {"status_filter": alias})
        )
        assert pend in text, f"{alias}: pending must appear; got {text}"
        assert prog in text, f"{alias}: in_progress must appear; got {text}"
        assert done not in text, f"{alias}: completed must be excluded; got {text}"


# ── unit: the shared matcher ─────────────────────────────────────────


async def test_status_filter_matches_unit() -> None:
    from agent_mcp.features.task_queries import status_filter_matches

    # incomplete aliases → any active status
    for alias in ("incomplete", "active", "open"):
        assert status_filter_matches(alias, "pending")
        assert status_filter_matches(alias, "in_progress")
        assert not status_filter_matches(alias, "completed")
        assert not status_filter_matches(alias, "cancelled")
        assert not status_filter_matches(alias, "failed")
    # concrete status → exact match
    assert status_filter_matches("pending", "pending")
    assert not status_filter_matches("pending", "in_progress")
    assert status_filter_matches("completed", "completed")


# ── created_by filter ────────────────────────────────────────────────


async def test_view_tasks_filter_by_creator(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        mine = f"task_{secrets.token_hex(6)}"
        theirs = f"task_{secrets.token_hex(6)}"
        _seed_task(mine, "filed by alice", created_by="alice")
        _seed_task(theirs, "filed by bob", created_by="bob")
        text = _text(await admin.call("view_tasks", {"created_by": "alice"}))
        assert mine in text, f"created_by=alice must include alice's task; got {text}"
        assert theirs not in text, "bob's task must be excluded"


async def test_search_tasks_filter_by_creator(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        mine = f"task_{secrets.token_hex(6)}"
        theirs = f"task_{secrets.token_hex(6)}"
        _seed_task(mine, "filed by alice", created_by="alice")
        _seed_task(theirs, "filed by bob", created_by="bob")
        # created_by alone is a valid filter-only listing (no query needed).
        text = _text(await admin.call("search_tasks", {"created_by": "alice"}))
        assert mine in text and theirs not in text


# ── unassigned filter + the "agent named unassigned" collision guard ──


async def test_view_tasks_unassigned_only(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        pool = f"task_{secrets.token_hex(6)}"
        owned = f"task_{secrets.token_hex(6)}"
        _seed_task(pool, "claimable", assigned_to=None)
        _seed_task(owned, "owned by carol", assigned_to="carol")
        text = _text(await admin.call("view_tasks", {"unassigned": True}))
        assert pool in text, f"unassigned=true must include the pool task; got {text}"
        assert owned not in text, "an assigned task must be excluded"


async def test_agent_named_unassigned_does_not_collide(tmp_path) -> None:
    """An agent literally named 'unassigned' must NOT be confused with the
    unassigned pool. ``unassigned=true`` is a boolean checking
    assigned_to IS NULL — never a magic assignee value."""
    async with mcp_session(tmp_path) as admin:
        real_pool = f"task_{secrets.token_hex(6)}"
        assigned_to_unassigned_agent = f"task_{secrets.token_hex(6)}"
        _seed_task(real_pool, "truly unassigned", assigned_to=None)
        _seed_task(
            assigned_to_unassigned_agent,
            "assigned to an agent named unassigned",
            assigned_to="unassigned",
        )

        # unassigned=true → only the truly-NULL task, NOT the one assigned
        # to the agent named "unassigned".
        pool_text = _text(await admin.call("view_tasks", {"unassigned": True}))
        assert real_pool in pool_text, "the NULL-assigned pool task must appear"
        assert assigned_to_unassigned_agent not in pool_text, (
            "a task assigned to an agent NAMED 'unassigned' must NOT be "
            "treated as unassigned — that's the collision bug we avoid"
        )

        # agent_id='unassigned' → the task assigned to that agent, NOT the
        # truly-unassigned pool task.
        agent_text = _text(
            await admin.call("view_tasks", {"agent_id": "unassigned"})
        )
        assert assigned_to_unassigned_agent in agent_text, (
            "agent_id=unassigned must match the task assigned to that agent"
        )
        assert real_pool not in agent_text, (
            "the truly-unassigned task must NOT match agent_id=unassigned"
        )


# ── assigned filter (complement of unassigned) ───────────────────────


async def test_view_tasks_assigned_only(tmp_path) -> None:
    """``assigned=true`` returns only tasks that HAVE an assignee."""
    async with mcp_session(tmp_path) as admin:
        owned = f"task_{secrets.token_hex(6)}"
        pool = f"task_{secrets.token_hex(6)}"
        _seed_task(owned, "owned by carol", assigned_to="carol")
        _seed_task(pool, "claimable", assigned_to=None)
        text = _text(await admin.call("view_tasks", {"assigned": True}))
        assert owned in text, f"assigned=true must include assigned tasks; got {text}"
        assert pool not in text, "assigned=true must exclude the unassigned pool"


async def test_worker_assigned_collapses_view_to_just_mine(tmp_path) -> None:
    """The gap this closes: a worker's view is {mine + pool}; assigned=true
    narrows it to just {mine} (the pool is excluded, others already hidden)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        mine = f"task_{secrets.token_hex(6)}"
        pool = f"task_{secrets.token_hex(6)}"
        _seed_task(mine, "assigned to alice", assigned_to="alice")
        _seed_task(pool, "claimable pool", assigned_to=None)

        # Default worker view: both mine + the pool.
        default_text = _text(await alice.call("view_tasks", {}))
        assert mine in default_text and pool in default_text, (
            "worker default view is {mine + pool}"
        )
        # assigned=true → just mine.
        assigned_text = _text(await alice.call("view_tasks", {"assigned": True}))
        assert mine in assigned_text
        assert pool not in assigned_text, (
            "assigned=true must drop the pool from a worker's view"
        )


async def test_worker_assigned_incomplete_is_my_open_tasks(tmp_path) -> None:
    """assigned=true + status=incomplete = 'my open tasks': excludes my
    completed tasks AND the unassigned pool."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        mine_open = f"task_{secrets.token_hex(6)}"
        mine_done = f"task_{secrets.token_hex(6)}"
        pool_open = f"task_{secrets.token_hex(6)}"
        _seed_task(mine_open, "my open", assigned_to="alice", status="in_progress")
        _seed_task(mine_done, "my done", assigned_to="alice", status="completed")
        _seed_task(pool_open, "pool open", assigned_to=None, status="pending")

        text = _text(
            await alice.call(
                "view_tasks", {"assigned": True, "status": "incomplete"}
            )
        )
        assert mine_open in text, "my in_progress task must appear"
        assert mine_done not in text, "my completed task must be excluded"
        assert pool_open not in text, "the pool must be excluded by assigned=true"
