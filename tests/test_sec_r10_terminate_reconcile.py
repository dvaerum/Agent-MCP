"""Round-10 coordination/state regressions.

BL-R10-1 [MED] — terminate/purge cascade leaves ``g.tasks`` cache
    stale. The bulk ``UPDATE tasks SET assigned_to=NULL,
    status='unassigned'`` reconciles the DB but not the in-memory
    ``state.tasks`` cache that ``view_tasks`` iterates, so a task
    orphaned by termination keeps showing as pinned to the (now dead)
    agent until a restart. Fix reconciles each reassigned row's cache
    entry post-commit — upsert (NOT evict), because the task still
    exists, just unassigned.

BL-R10-2 [MED] — tasks orphaned by termination are invisible to a
    worker's catch-up feed. ``_collect_unassigned_task_events_for``
    keyed the catch-up query on ``created_at``, but a task orphaned by
    terminate/purge keeps its ORIGINAL creation time; its meaningful
    transition time is ``updated_at``. Fix keys the query (and the event
    timestamp) on ``updated_at`` and pushes
    ``notify_unassigned_task_appeared`` for each reassigned task so live
    waiters wake.
"""

from __future__ import annotations

import datetime

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _operator_principal(project_name: str = "demo-project") -> Principal:
    return make_principal(
        kind="operator_session",
        user_id="test-operator",
        agent_id=None,
        sysadmin=False,
        project_name=project_name,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _insert_task(
    *,
    task_id: str,
    assigned_to,
    status: str = "pending",
    created_by: str = "admin",
    created_at: str | None = None,
    updated_at: str | None = None,
) -> None:
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Task

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Task(
                task_id=task_id,
                title=f"task {task_id}",
                description=None,
                assigned_to=assigned_to,
                created_by=created_by,
                status=status,
                priority="medium",
                created_at=created_at or now,
                updated_at=updated_at or now,
            )
        )
        session.commit()


def _warm_cache(task_id: str) -> None:
    """Mirror the boot-time load: put the DB-authoritative row into
    ``state.tasks`` so ``view_tasks`` (which reads the cache) sees it."""
    from agent_mcp.repositories import task_repo
    from agent_mcp.repositories.task_repository import get_task_by_id

    row = get_task_by_id(task_id)
    assert row is not None
    task_repo.upsert_cache(row)


# ── BL-R10-1: terminate reconciles the g.tasks cache ─────────────────


async def test_terminate_reconciles_task_cache(tmp_path):
    """After terminate, ``g.tasks`` shows the orphaned task as
    unassigned — and the task is still present (upsert, not evict)."""
    from agent_mcp.core import globals as g
    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        terminate_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        reg = await register_agent_tool_impl(
            {"name": "wkr-cache-term", "role": "worker", "host": "https://h.x"},
            principal=_operator_principal(),
        )
        assert isinstance(reg, Ok)
        agent_id = reg.data["agent_id"]

        _insert_task(task_id="tc-1", assigned_to=agent_id, status="pending")
        _warm_cache("tc-1")
        # Precondition: cache pins the task to the (soon-dead) agent.
        assert g.tasks["tc-1"]["assigned_to"] == agent_id

        term = await terminate_agent_tool_impl(
            {"agent_id": agent_id}, principal=_operator_principal(),
        )
        assert isinstance(term, Ok)

        assert "tc-1" in g.tasks, (
            "task must remain in cache (upsert, not evict) so view_tasks "
            "still lists it as unassigned"
        )
        assert g.tasks["tc-1"]["assigned_to"] is None, (
            "cache still pins the task to the terminated agent"
        )
        assert g.tasks["tc-1"]["status"] == "unassigned"


async def test_terminate_cache_reconcile_visible_in_view_tasks(tmp_path):
    """End-to-end: view_tasks no longer reports the orphaned task as
    assigned to the terminated agent."""
    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        terminate_agent_tool_impl,
    )
    from agent_mcp.tools.task_tools import view_tasks_tool_impl

    async with mcp_session(tmp_path):
        reg = await register_agent_tool_impl(
            {"name": "wkr-view-term", "role": "worker", "host": "https://h.x"},
            principal=_operator_principal(),
        )
        assert isinstance(reg, Ok)
        agent_id = reg.data["agent_id"]

        _insert_task(task_id="tv-1", assigned_to=agent_id, status="pending")
        _warm_cache("tv-1")

        await terminate_agent_tool_impl(
            {"agent_id": agent_id}, principal=_operator_principal(),
        )

        result = await view_tasks_tool_impl(
            {"agent_id": agent_id}, principal=_operator_principal(),
        )
        assert isinstance(result, Ok)
        # The dead agent owns nothing now; the task is unassigned.
        assert f"Assigned to: {agent_id}" not in result.message


# ── BL-R10-1: purge reconciles the g.tasks cache ─────────────────────


async def test_purge_reconciles_task_cache(tmp_path):
    """Same invariant as terminate, on the purge (hard-delete) path."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("wkr-cache-purge")
        agent_id = worker.agent_id

        _insert_task(task_id="pc-1", assigned_to=agent_id, status="pending")
        _warm_cache("pc-1")
        assert g.tasks["pc-1"]["assigned_to"] == agent_id

        resp = admin.request(
            "DELETE",
            f"/api/agents/{agent_id}",
            params={"cascade": "true"},
            json={},
        )
        assert resp.status_code == 200, resp.text

        assert "pc-1" in g.tasks
        assert g.tasks["pc-1"]["assigned_to"] is None
        assert g.tasks["pc-1"]["status"] == "unassigned"


# ── BL-R10-2: catch-up feed keys on updated_at, not created_at ───────


async def test_catchup_surfaces_task_unassigned_after_cursor(tmp_path):
    """A task CREATED before the caller's cursor but UNASSIGNED after it
    must surface in the catch-up feed — the query keys on updated_at
    (transition time), not created_at (original creation time)."""
    from agent_mcp.tools.agent_communication_tools import (
        _collect_unassigned_task_events_for,
    )

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("wkr-catchup")
        agent_id = worker.agent_id

        base = datetime.datetime(2026, 1, 1, 12, 0, 0)
        created = base.isoformat()                       # T0
        cursor = (base + datetime.timedelta(hours=1)).isoformat()   # T1
        transitioned = (base + datetime.timedelta(hours=2)).isoformat()  # T2

        # Unassigned task, empty required caps (matches everyone),
        # created at T0 but transitioned to unassigned at T2.
        _insert_task(
            task_id="cu-1",
            assigned_to=None,
            status="unassigned",
            created_at=created,
            updated_at=transitioned,

        )

        events = _collect_unassigned_task_events_for(agent_id, cursor)
        ids = [e["ref_id"] for e in events]
        assert "cu-1" in ids, (
            "task transitioned to unassigned AFTER the cursor must "
            "surface; the query must key on updated_at, not created_at"
        )
        # Cursor semantics: the event timestamp is the transition time so
        # the caller's cursor advances past it (no infinite re-surface).
        evt = next(e for e in events if e["ref_id"] == "cu-1")
        assert evt["timestamp"] == transitioned


async def test_catchup_excludes_task_transitioned_before_cursor(tmp_path):
    """Guard the other boundary: a task whose updated_at is at/older than
    the cursor is NOT returned (no re-surface once the cursor advances)."""
    from agent_mcp.tools.agent_communication_tools import (
        _collect_unassigned_task_events_for,
    )

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("wkr-catchup2")
        agent_id = worker.agent_id

        base = datetime.datetime(2026, 1, 1, 12, 0, 0)
        old = base.isoformat()
        cursor = (base + datetime.timedelta(hours=1)).isoformat()

        _insert_task(
            task_id="cu-old",
            assigned_to=None,
            status="unassigned",
            created_at=old,
            updated_at=old,

        )

        events = _collect_unassigned_task_events_for(agent_id, cursor)
        assert "cu-old" not in [e["ref_id"] for e in events]


async def test_terminate_orphaned_task_surfaces_in_fetch_events(tmp_path):
    """End-to-end BL-R10-2: after terminating an agent, a DIFFERENT
    worker's fetch_events_since (cursor before the unassign) surfaces the
    now-unassigned task."""
    import json as _json

    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        terminate_agent_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        # Doomed agent holding a task, created well before the cursor.
        reg = await register_agent_tool_impl(
            {"name": "wkr-doomed", "role": "worker", "host": "https://h.x"},
            principal=_operator_principal(),
        )
        assert isinstance(reg, Ok)
        doomed_id = reg.data["agent_id"]

        old = datetime.datetime(2026, 1, 1, 12, 0, 0).isoformat()
        _insert_task(
            task_id="fe-1",
            assigned_to=doomed_id,
            status="pending",
            created_at=old,
            updated_at=old,

        )
        _warm_cache("fe-1")

        # A live observer worker with a cursor AFTER the task's creation.
        observer = await admin.create_worker("wkr-observer")
        cursor = datetime.datetime(2026, 1, 1, 13, 0, 0).isoformat()

        await terminate_agent_tool_impl(
            {"agent_id": doomed_id}, principal=_operator_principal(),
        )

        blocks = await observer.call(
            "fetch_events_since", {"cursor": cursor},
        )
        text = blocks[0].text if hasattr(blocks[0], "text") else str(blocks)
        body = _json.loads(text)
        ids = [
            e["ref_id"]
            for e in body["events"]
            if e["type"] == "unassigned_task_appeared"
        ]
        assert "fe-1" in ids, (
            "task orphaned by terminate must surface in a worker's "
            f"catch-up feed; got {body}"
        )
