"""Cache-vs-DB invariant tests for TaskRepository (PR-W2c).

The repo owns the in-memory ``state.tasks`` cache + the DB writes for
the ``tasks`` table. These tests pin the four invariants spelled out
in the spec:

A. Write-through: ``repo.create_task(...)`` updates the cache
   immediately so the next ``repo.get_task(...)`` is a cache hit.
B. Cache-miss path: a DB-direct insert (bypass repo) is reflected on
   the next repo read (fall-through to DB).
C. EventBus integration (no-op if bus module absent): ``update_task_status``
   publishes to the bus when ``agent_mcp.core.event_bus`` is importable.
D. Test mode: ``disable_cache()`` makes the repo skip the in-memory
   cache entirely so reads go straight to DB.

The repo MUST keep ``state.tasks`` in sync so legacy callers reading
``state.tasks[task_id]`` directly still see the up-to-date value (the
shim stays until a follow-up PR can mechanically delete the state
cache once no callers remain).
"""

from __future__ import annotations

import datetime

from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _insert_task_via_db(task_id: str, *, status: str = "pending") -> None:
    """Bypass-the-repo direct DB insert for the cache-miss test."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Task

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Task(
                task_id=task_id,
                title=f"direct {task_id}",
                description="inserted bypassing repo",
                assigned_to=None,
                created_by="admin",
                status=status,
                priority="medium",
                created_at=now,
                updated_at=now,
                parent_task=None,
                child_tasks="[]",
                depends_on_tasks="[]",
                notes="[]",
            )
        )
        session.commit()


def test_create_task_updates_cache_immediately(project_dir, reset_globals):
    """Test A: write-through invariant for create_task."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.core.repositories import task_repo

        task_id = "task-write-through"
        task_repo.create_task(
            task_id=task_id,
            title="hello",
            description="cache-write-through",
            created_by="admin",
            status="pending",
            priority="high",
        )

        # Cache reflects new value immediately.
        assert task_id in state.tasks, (
            "create_task must populate state.tasks cache"
        )
        # And the repo getter returns it without hitting the DB.
        got = task_repo.get_task(task_id)
        assert got is not None
        assert got["title"] == "hello"
        assert got["status"] == "pending"


def test_db_direct_insert_visible_on_next_repo_read(project_dir, reset_globals):
    """Test B: cache-miss path falls through to DB."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.core.repositories import task_repo

        # Sanity: cache empty initially.
        assert "task-db-direct" not in state.tasks

        _insert_task_via_db("task-db-direct", status="pending")

        # Cache is still empty (we bypassed the repo) ...
        assert "task-db-direct" not in state.tasks
        # ... but the repo getter falls through to DB and returns it.
        got = task_repo.get_task("task-db-direct")
        assert got is not None
        assert got["task_id"] == "task-db-direct"
        # And populates the cache as a side effect (warm-on-miss).
        assert "task-db-direct" in state.tasks


def test_update_task_status_publishes_event(
    project_dir, reset_globals, monkeypatch
):
    """Test C: write-through mutator publishes to EventBus if available.

    The bus is a soft dependency (W2b lands in parallel). When the
    module is importable we expect ``bus.notify(agent_id, ...)`` to be
    called; otherwise the repo must no-op silently.
    """
    captured: list[tuple[str, str, dict]] = []

    class _FakeBus:
        @staticmethod
        def notify(agent_id, event_type, payload):  # noqa: D401, ANN001
            captured.append((agent_id, event_type, payload))

    # Inject a fake event_bus module that the repo can import.
    import sys

    sys.modules["agent_mcp.core.event_bus"] = _FakeBus()  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core.repositories import agent_repo, task_repo

            # tasks.assigned_to has a FK to agents.agent_id (PR #96),
            # so the assignee row must exist before the task.
            agent_repo.create_agent(
                token="tok-w-a",
                agent_id="worker-a",
                capabilities=[],
                status="active",
                working_directory="/tmp/wa",
                color="#000000",
            )

            task_repo.create_task(
                task_id="task-event-bus",
                title="bus test",
                description="x",
                assigned_to="worker-a",
                created_by="admin",
                status="pending",
                priority="medium",
            )
            captured.clear()

            task_repo.update_task_status(
                task_id="task-event-bus",
                new_status="in_progress",
                updated_by="worker-a",
            )

            # The bus saw the event addressed to the assignee.
            assert captured, "EventBus must receive status updates"
            agent_id, event_type, _payload = captured[0]
            assert agent_id == "worker-a"
            assert "task" in event_type.lower()
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_disable_cache_skips_in_memory(project_dir, reset_globals):
    """Test D: ``disable_cache()`` test-mode bypasses the cache."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.core.repositories import task_repo

        with task_repo.disable_cache():
            task_repo.create_task(
                task_id="task-no-cache",
                title="nocache",
                description="x",
                created_by="admin",
                status="pending",
                priority="low",
            )
            # In disable_cache mode, state.tasks must NOT carry the row.
            assert "task-no-cache" not in state.tasks
            # ... but the row is in the DB and a repo read returns it.
            got = task_repo.get_task("task-no-cache")
            assert got is not None
            assert got["title"] == "nocache"
            # ... still no cache pollution.
            assert "task-no-cache" not in state.tasks
