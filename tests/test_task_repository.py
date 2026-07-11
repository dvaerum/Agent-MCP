"""Contract tests for the class-based ``TaskRepository`` (PR #146).

PR #137 introduced module-of-functions repositories under
``agent_mcp.core.repositories``. The architecture review
(2026-06-09) found that the ``Repository`` name was metaphorical;
this PR transforms the contract into a real ``class TaskRepository``
exposed as a lifespan-owned singleton on ``agent_mcp.repositories``.

What this test file pins:

* The singleton exists at ``agent_mcp.repositories.task_repo`` after
  application lifespan startup, and points at a ``TaskRepository``
  instance.
* Every method preserves the wire-equivalent semantics of the legacy
  function surface (return shapes, cache invariants, EventBus
  publishing) so call-site migrations are mechanical.
* Two methods that are net-new in PR #146 — ``delete`` and
  ``bulk_update_fields`` — earn their own tests. The bulk variant
  exists to mitigate the EventBus-storm risk surfaced in grilling
  (a 20-update loop calling ``update_fields`` 20 times would emit 20
  events; ``bulk_update_fields`` collapses that to one).

These tests fail on ``main`` because:

* ``agent_mcp.repositories`` (top-level package) does not yet exist —
  only ``agent_mcp.core.repositories`` does.
* ``TaskRepository`` (the class) does not exist.
* ``TaskRepository.delete`` and ``TaskRepository.bulk_update_fields``
  are net-new.

The fixture topology re-uses the existing ``project_dir`` +
``reset_globals`` pattern so this test file slots straight into the
existing in-process app harness — no new harness changes needed.
"""

from __future__ import annotations

import datetime
import sys

import pytest

from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient


# --- Helpers -------------------------------------------------------------


def _make_client(project_dir):
    """Build the in-process app + TestClient.

    Using a fresh client per test means each call runs through the
    full lifespan startup (which is what wires the singleton in
    PR #146).
    """
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _seed_task(
    task_id: str,
    *,
    title: str = "seed",
    description: str = "seeded by test",
    assigned_to: str | None = None,
    created_by: str = "admin",
    status: str = "pending",
    priority: str = "medium",
):
    """Insert a task via the existing ORM path (bypassing the repo).

    The repo class under test must observe this row when callers ask
    for it — proves the read methods fall through to the DB rather
    than returning only what passed through ``create``.
    """
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Task

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Task(
                task_id=task_id,
                title=title,
                description=description,
                assigned_to=assigned_to,
                created_by=created_by,
                status=status,
                priority=priority,
                created_at=now,
                updated_at=now,
                parent_task=None,
                child_tasks="[]",
                depends_on_tasks="[]",
                notes="[]",
            )
        )
        session.commit()


class _CapturingBus:
    """Drop-in replacement for ``agent_mcp.core.event_bus``.

    Captures every ``(agent_id, event_type, payload)`` tuple so a
    test can assert exactly one publish — used to pin the bulk
    variant doesn't degenerate into a per-row publish loop.
    """

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def notify(self, agent_id, event_type, payload):  # noqa: D401, ANN001
        self.events.append((agent_id, event_type, payload or {}))


# --- Singleton + lifespan wiring ----------------------------------------


def test_task_repo_singleton_is_taskrepository_instance(
    project_dir, reset_globals,
):
    """``agent_mcp.repositories.task_repo`` resolves to a class instance.

    The plan locks "module singletons, lifespan-owned" — so the
    attribute access shape is ``from agent_mcp.repositories import
    task_repo`` and the value is an instance, not a module.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo
        from agent_mcp.repositories.task_repository import TaskRepository

        assert isinstance(task_repo, TaskRepository), (
            "task_repo must be a TaskRepository instance after lifespan "
            "startup so call sites can rely on the class-based contract"
        )


# --- Read interface ------------------------------------------------------


def test_get_by_id_returns_dict_when_present(project_dir, reset_globals):
    """``get_by_id`` returns the same dict shape the legacy callers expect."""
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo

        _seed_task("task-getbyid", title="present")

        row = task_repo.get_by_id("task-getbyid")
        assert row is not None
        assert row["task_id"] == "task-getbyid"
        assert row["title"] == "present"
        # JSON list fields are deserialised — preserves legacy projection.
        assert row["notes"] == []
        assert row["child_tasks"] == []


def test_get_by_id_returns_none_when_missing(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo
        assert task_repo.get_by_id("does-not-exist") is None


def test_list_all_returns_every_task(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo

        _seed_task("task-a", title="A")
        _seed_task("task-b", title="B")

        ids = {row["task_id"] for row in task_repo.list_all()}
        assert ids >= {"task-a", "task-b"}


def test_list_by_agent_without_status_filter(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        from agent_mcp.repositories import task_repo

        # FK from tasks.assigned_to -> agents.agent_id requires the
        # agent row first (PR #96 declared the constraint).
        agent_repo.create(
            token="tok-w1",
            agent_id="worker-1",
            capabilities=[],
            status="active",
            working_directory="/tmp/w1",
            color="#111111",
        )

        _seed_task("task-w1-a", assigned_to="worker-1", status="pending")
        _seed_task("task-w1-b", assigned_to="worker-1", status="completed")

        rows = task_repo.list_by_agent("worker-1")
        ids = {row["task_id"] for row in rows}
        assert ids == {"task-w1-a", "task-w1-b"}


def test_list_by_agent_with_status_filter(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        from agent_mcp.repositories import task_repo

        agent_repo.create(
            token="tok-w2",
            agent_id="worker-2",
            capabilities=[],
            status="active",
            working_directory="/tmp/w2",
            color="#222222",
        )
        _seed_task("task-w2-a", assigned_to="worker-2", status="pending")
        _seed_task("task-w2-b", assigned_to="worker-2", status="completed")

        rows = task_repo.list_by_agent("worker-2", status_filter="pending")
        ids = {row["task_id"] for row in rows}
        assert ids == {"task-w2-a"}


# --- Write interface: create --------------------------------------------


def test_create_returns_dict_and_updates_cache_and_publishes(
    project_dir, reset_globals,
):
    """``create`` is the single seam for new tasks.

    Contract:
      1. Returns the freshly-created dict (not just bool).
      2. Cache (``state.tasks``) carries the new row so the next
         read is a cache hit.
      3. EventBus sees exactly one publish for the new task.
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import task_repo

            entity = task_repo.create(
                {
                    "task_id": "task-create",
                    "title": "create test",
                    "description": "x",
                    "created_by": "admin",
                    "status": "pending",
                    "priority": "high",
                }
            )

            assert entity["task_id"] == "task-create"
            assert entity["title"] == "create test"
            assert "task-create" in state.tasks, (
                "create must populate the in-memory cache (single ownership "
                "of cache+DB invariant)"
            )
            # Exactly one publish for the create.
            create_events = [
                e for e in bus.events if "task" in e[1] and "created" in e[1]
            ]
            assert len(create_events) == 1, bus.events
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_create_mints_task_id_when_omitted(project_dir, reset_globals):
    """arch-deepening R4 #7: ``task_id`` is now optional.

    When the caller omits it, ``create`` mints one via the opaque,
    ``secrets``-based scheme — retiring the two
    ``task_{int(now().timestamp()*1000)}`` generators that used to
    live in ``tools/task_tools.py`` (single-unassigned and
    multi-create paths), which could collide within the same
    millisecond and raise a duplicate-PK ``IntegrityError``.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo

        entity = task_repo.create({"title": "no explicit id", "created_by": "admin"})

        assert entity["task_id"], "create must mint a task_id when omitted"
        assert entity["task_id"].startswith("task_")


def test_create_minted_ids_unique_across_rapid_creates(project_dir, reset_globals):
    """The minted scheme doesn't collide across a tight back-to-back
    batch — the same shape of load that used to trip the retired
    timestamp generators when two calls landed in the same
    millisecond (see ``tests/test_arch_r4_7_task_row_factory.py`` for
    the tool-call-boundary reproduction with a frozen clock).
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo

        ids = [
            task_repo.create({"title": f"batch {i}", "created_by": "admin"})[
                "task_id"
            ]
            for i in range(50)
        ]

        assert len(ids) == len(set(ids)), f"minted task_ids collided: {ids}"


def test_create_default_row_shape(project_dir, reset_globals):
    """Single-sourced default row shape.

    A caller that supplies only the required fields (``title``,
    ``created_by``) gets the SAME defaults every ``create()`` call
    site used to hand-list explicitly (``child_tasks`` /
    ``depends_on_tasks`` / ``notes`` = ``[]``, ``status`` =
    ``"pending"``, ``priority`` = ``"medium"``,
    ``required_capabilities`` = ``None``) — one assertion here instead
    of trusting ~7 near-identical call-site dict literals to agree.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo

        entity = task_repo.create({"title": "defaults only", "created_by": "admin"})

        assert entity["child_tasks"] == []
        assert entity["depends_on_tasks"] == []
        assert entity["notes"] == []
        assert entity["status"] == "pending"
        assert entity["priority"] == "medium"
        assert entity["required_capabilities"] is None


def test_create_duplicate_id_raises(project_dir, reset_globals):
    """Inserting a row with a duplicate primary key must surface an error.

    Silently returning the existing row would mask write conflicts —
    the legacy SQL path raised IntegrityError; the class contract
    preserves that.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo

        fields = {
            "task_id": "task-dup",
            "title": "first",
            "description": "x",
            "created_by": "admin",
            "status": "pending",
            "priority": "low",
        }
        task_repo.create(fields)

        with pytest.raises(Exception):
            task_repo.create(fields)


# --- Write interface: update_fields -------------------------------------


def test_update_fields_success_emits_event_and_updates_cache(
    project_dir, reset_globals,
):
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import task_repo

            _seed_task("task-upd", title="before")
            bus.events.clear()

            result = task_repo.update_fields(
                "task-upd", {"title": "after", "status": "completed"},
            )

            assert result is not None
            assert result["title"] == "after"
            assert result["status"] == "completed"

            # Cache reflects new values.
            cached = state.tasks.get("task-upd")
            assert cached is not None
            assert cached["title"] == "after"

            # Exactly one event from this update_fields call.
            assert len(bus.events) == 1, bus.events
            _agent_id, event_type, _payload = bus.events[0]
            assert "task" in event_type and "update" in event_type
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_update_fields_missing_task_returns_none(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo
        result = task_repo.update_fields("nope", {"title": "x"})
        assert result is None


# --- Write interface: delete --------------------------------------------


def test_delete_success_returns_true_and_evicts_cache_and_publishes(
    project_dir, reset_globals,
):
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import task_repo

            _seed_task("task-del", title="bye")
            # Prime the cache so we can confirm eviction.
            task_repo.get_by_id("task-del")
            assert "task-del" in state.tasks
            bus.events.clear()

            ok = task_repo.delete("task-del")
            assert ok is True

            # Cache evicted.
            assert "task-del" not in state.tasks
            # Row gone from DB.
            assert task_repo.get_by_id("task-del") is None
            # Exactly one delete event.
            delete_events = [e for e in bus.events if "delete" in e[1]]
            assert len(delete_events) == 1, bus.events
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_delete_missing_returns_false(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import task_repo
        assert task_repo.delete("missing") is False


# --- Write interface: bulk_update_fields (Risk #2 mitigation) -----------


def test_bulk_update_fields_emits_single_event_per_invocation(
    project_dir, reset_globals,
):
    """``bulk_update_fields`` collapses an N-row update to one publish.

    The plan's Risk #2 ("EventBus storm") motivates this method: a
    loop calling ``update_fields`` 20 times emits 20 events; the
    bulk variant emits one. We assert exactly one event regardless
    of row count.
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import task_repo

            _seed_task("bulk-a", status="pending")
            _seed_task("bulk-b", status="pending")
            _seed_task("bulk-c", status="pending")
            bus.events.clear()

            updated = task_repo.bulk_update_fields(
                ["bulk-a", "bulk-b", "bulk-c"],
                {"status": "in_progress"},
            )

            # All three rows updated; cache reflects new status.
            assert sorted(row["task_id"] for row in updated) == [
                "bulk-a", "bulk-b", "bulk-c",
            ]
            for tid in ("bulk-a", "bulk-b", "bulk-c"):
                assert state.tasks.get(tid, {}).get("status") == "in_progress"

            # Exactly ONE event for the whole bulk operation.
            bulk_events = [e for e in bus.events if "task" in e[1]]
            assert len(bulk_events) == 1, (
                "bulk_update_fields must emit a single batched event, not "
                f"one per row. Got: {bus.events}"
            )
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


# --- Transaction-aware seam on create / delete (PR #152) ----------------


def test_create_with_sqlite_cursor_lands_in_caller_transaction(
    project_dir, reset_globals,
):
    """``create(connection=cursor)`` writes through the caller's
    sqlite3 cursor so the row lands inside the caller's BEGIN/COMMIT.

    This pins the seam the admin_tools / task_tools migration relies
    on to keep the task INSERT atomic with the surrounding
    ``agent_actions`` audit-log INSERT.
    """
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import task_repo

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            fresh = task_repo.create(
                {
                    "task_id": "task_seam_1",
                    "title": "via cursor",
                    "description": "seam test",
                    "created_by": "admin",
                },
                connection=cursor,
            )
            assert fresh["task_id"] == "task_seam_1"
            # Visible to OTHER connections only after commit.
            conn.commit()
        finally:
            conn.close()

        row = task_repo.get_by_id("task_seam_1")
        assert row is not None
        assert row["title"] == "via cursor"


def test_create_with_sqlite_cursor_rolls_back_with_outer_transaction(
    project_dir, reset_globals,
):
    """If the caller's transaction rolls back, the repo-created row
    must NOT persist — proves the INSERT really uses the caller's
    transaction (not a hidden session that commits on its own)."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import task_repo

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            task_repo.create(
                {
                    "task_id": "task_seam_rollback",
                    "title": "should not persist",
                    "created_by": "admin",
                },
                connection=cursor,
            )
            conn.rollback()
        finally:
            conn.close()

        # No row should exist because the outer transaction rolled back.
        assert task_repo.get_by_id("task_seam_rollback") is None


def test_delete_with_sqlite_cursor_uses_caller_transaction(
    project_dir, reset_globals,
):
    """``delete(connection=cursor)`` removes the row through the
    caller's cursor, returns True, and the deletion is atomic with
    the caller's transaction.

    The caller is responsible for evicting the cache after their own
    commit (the repo defers cache+publish on the connection= path so
    a rollback can't leave the cache desynced). This test exercises
    the caller-owns-cache contract explicitly.
    """
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import task_repo

        _seed_task("task_seam_delete")
        # Don't warm the cache from a different path — the test
        # validates the DB delete + caller-driven eviction, not the
        # cache-warm path.

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            ok = task_repo.delete(
                "task_seam_delete", connection=cursor,
            )
            assert ok is True
            conn.commit()
        finally:
            conn.close()

        # Caller-owns-cache: evict after commit. This is what the
        # admin_tools/task_tools migration sites are expected to do.
        task_repo.evict_from_cache("task_seam_delete")
        assert task_repo.get_by_id("task_seam_delete") is None


def test_delete_with_sqlite_cursor_returns_false_for_missing_row(
    project_dir, reset_globals,
):
    """A delete for a non-existent task returns False without raising,
    even on the cursor seam path."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import task_repo

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            ok = task_repo.delete("does_not_exist", connection=cursor)
            assert ok is False
            conn.commit()
        finally:
            conn.close()
