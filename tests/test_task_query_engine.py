"""TaskQueryEngine contract tests.

This is PR D of the round-2 architecture-review series — the engine
extracts the **business rules** (filtering, sorting, pagination, health
scoring) buried inside ``view_tasks_tool_impl`` so they're unit-testable
without the LLM-text fixtures the handler returns.

The engine owns:

* filter rules (status / priority / agent / parent / blocked-state)
* sort rules (priority, status, created_at, updated_at)
* pagination (offset + limit)
* dependency analysis (which tasks are blocked, what's blocking, what
  this task blocks)
* health metrics (counts, score, status grade)

Presentation (emoji, smart tips, token budgeting, response shaping)
stays at the caller — those concerns are not exercised here.

These tests run against the engine **directly**, not through the MCP
handler, so a regression in the rules can be diagnosed without
chasing the response-text adapter.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict

import pytest

from agent_mcp.features.task_queries import (
    TaskFilterSpec,
    TaskQueryEngine,
    TaskSortSpec,
)


# --- Fixtures ---------------------------------------------------------


def _task(
    task_id: str,
    *,
    status: str = "pending",
    priority: str = "medium",
    assigned_to: str | None = None,
    parent_task: str | None = None,
    depends_on_tasks: list[str] | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    title: str | None = None,
) -> Dict[str, Any]:
    """Build a synthetic task row matching ``g.tasks`` shape."""
    base = _dt.datetime(2025, 1, 1)
    return {
        "task_id": task_id,
        "title": title or f"task-{task_id}",
        "description": "synthetic",
        "status": status,
        "priority": priority,
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": created_at or base.isoformat(),
        "updated_at": updated_at or created_at or base.isoformat(),
        "parent_task": parent_task,
        "child_tasks": [],
        "depends_on_tasks": depends_on_tasks or [],
        "notes": [],
    }


@pytest.fixture
def snapshot() -> Dict[str, Dict[str, Any]]:
    """A 10-task fixture covering the rule matrix."""
    base = _dt.datetime(2025, 1, 1)
    tasks = {
        "t1": _task(
            "t1",
            status="pending",
            priority="high",
            assigned_to="alice",
            created_at=(base + _dt.timedelta(minutes=1)).isoformat(),
        ),
        "t2": _task(
            "t2",
            status="in_progress",
            priority="high",
            assigned_to="alice",
            created_at=(base + _dt.timedelta(minutes=2)).isoformat(),
        ),
        "t3": _task(
            "t3",
            status="completed",
            priority="medium",
            assigned_to="alice",
            created_at=(base + _dt.timedelta(minutes=3)).isoformat(),
        ),
        "t4": _task(
            "t4",
            status="pending",
            priority="low",
            assigned_to="bob",
            created_at=(base + _dt.timedelta(minutes=4)).isoformat(),
        ),
        "t5": _task(
            "t5",
            status="failed",
            priority="high",
            assigned_to="bob",
            created_at=(base + _dt.timedelta(minutes=5)).isoformat(),
        ),
        # t6 depends on t3 (completed) and t5 (failed) — blocked
        "t6": _task(
            "t6",
            status="pending",
            priority="medium",
            assigned_to="alice",
            depends_on_tasks=["t3", "t5"],
            created_at=(base + _dt.timedelta(minutes=6)).isoformat(),
        ),
        # t7 child of t2
        "t7": _task(
            "t7",
            status="pending",
            priority="medium",
            assigned_to="alice",
            parent_task="t2",
            created_at=(base + _dt.timedelta(minutes=7)).isoformat(),
        ),
        # t8 stale (no update in >7 days, still pending)
        "t8": _task(
            "t8",
            status="pending",
            priority="medium",
            assigned_to="bob",
            created_at=(base - _dt.timedelta(days=30)).isoformat(),
            updated_at=(base - _dt.timedelta(days=30)).isoformat(),
        ),
        # t9 cancelled
        "t9": _task(
            "t9",
            status="cancelled",
            priority="low",
            assigned_to="bob",
            created_at=(base + _dt.timedelta(minutes=9)).isoformat(),
        ),
        # t10 depends on t1 (pending) — also blocked while t1 not done
        "t10": _task(
            "t10",
            status="pending",
            priority="medium",
            assigned_to="alice",
            depends_on_tasks=["t1"],
            created_at=(base + _dt.timedelta(minutes=10)).isoformat(),
        ),
    }
    return tasks


@pytest.fixture
def engine(snapshot: Dict[str, Dict[str, Any]]) -> TaskQueryEngine:
    """Engine bound to the fixture snapshot.

    The engine accepts a callable ``task_source`` that returns the
    current ``Dict[task_id, task_row]`` view. In production this points
    at ``g.tasks`` (or the repository). For tests we point at the
    fixture dict directly so we can mutate it inside a test (the
    concurrency / snapshot test does exactly that).
    """
    return TaskQueryEngine(task_source=lambda: snapshot)


# --- 1. Filter rules --------------------------------------------------


def test_filter_by_single_status(engine: TaskQueryEngine) -> None:
    result = engine.query(filters=TaskFilterSpec(status="pending"))
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"t1", "t4", "t6", "t7", "t8", "t10"}, ids
    assert result.total_count == len(ids)


def test_filter_by_priority(engine: TaskQueryEngine) -> None:
    result = engine.query(filters=TaskFilterSpec(priority="high"))
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"t1", "t2", "t5"}, ids


def test_filter_by_agent(engine: TaskQueryEngine) -> None:
    result = engine.query(filters=TaskFilterSpec(agent_id="alice"))
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"t1", "t2", "t3", "t6", "t7", "t10"}, ids


def test_filter_by_parent_returns_children(engine: TaskQueryEngine) -> None:
    result = engine.query(filters=TaskFilterSpec(parent_task_id="t2"))
    ids = [t["task_id"] for t in result.tasks]
    assert ids == ["t7"], ids


def test_filter_blocked_only(engine: TaskQueryEngine) -> None:
    """``blocked_only=True`` must return only tasks whose deps are not
    all completed.  t6 depends on t3 (completed) + t5 (failed) -> blocked.
    t10 depends on t1 (pending) -> blocked.
    """
    result = engine.query(filters=TaskFilterSpec(blocked_only=True))
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"t6", "t10"}, ids


def test_filter_combined_status_and_agent(engine: TaskQueryEngine) -> None:
    result = engine.query(
        filters=TaskFilterSpec(status="pending", agent_id="bob")
    )
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"t4", "t8"}, ids


def test_filter_by_agent_excludes_unassigned_by_default() -> None:
    """Default ``agent_id`` filter is strict equality: an unassigned
    (``assigned_to IS NULL``) row does NOT match — the pre-fix behavior
    stays the default for admins filtering by a specific agent."""
    snap = {
        "own": _task("own", assigned_to="alice"),
        "pool": _task("pool", assigned_to=None),
        "other": _task("other", assigned_to="bob"),
    }
    engine = TaskQueryEngine(task_source=lambda: snap)
    result = engine.query(filters=TaskFilterSpec(agent_id="alice"))
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"own"}, ids


def test_filter_include_unassigned_widens_to_pool() -> None:
    """``include_unassigned=True`` widens an ``agent_id`` filter to
    'my tasks OR the unassigned pool' — the worker visibility rule. A
    foreign-owned row (assigned to another agent) still never matches,
    preserving cross-worker isolation."""
    snap = {
        "own": _task("own", assigned_to="alice"),
        "pool_null": _task("pool_null", assigned_to=None),
        "pool_empty": _task("pool_empty", assigned_to=""),
        "other": _task("other", assigned_to="bob"),
    }
    engine = TaskQueryEngine(task_source=lambda: snap)
    result = engine.query(
        filters=TaskFilterSpec(agent_id="alice", include_unassigned=True)
    )
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"own", "pool_null", "pool_empty"}, ids
    assert "other" not in ids, "cross-worker isolation regressed"


# --- 2. Sort rules ----------------------------------------------------


def test_sort_by_priority_descending(engine: TaskQueryEngine) -> None:
    result = engine.query(sort=TaskSortSpec(by="priority"))
    # high tasks first (t1, t2, t5), then mediums, then lows
    priorities = [t["priority"] for t in result.tasks]
    high_idx = [i for i, p in enumerate(priorities) if p == "high"]
    low_idx = [i for i, p in enumerate(priorities) if p == "low"]
    assert max(high_idx) < min(low_idx), priorities


def test_sort_by_created_at_default_newest_first(
    engine: TaskQueryEngine,
) -> None:
    """The legacy handler default sorts ``created_at`` descending."""
    result = engine.query(sort=TaskSortSpec(by="created_at"))
    ids = [t["task_id"] for t in result.tasks]
    # newest at the top — t10 has the latest created_at (minute 10)
    # t8 is the oldest (-30 days), so it must be last
    assert ids[-1] == "t8", ids


# --- 3. Pagination ----------------------------------------------------


def test_paginate_offset_and_limit(engine: TaskQueryEngine) -> None:
    full = engine.query(sort=TaskSortSpec(by="created_at"))
    page = engine.query(sort=TaskSortSpec(by="created_at"), offset=2, limit=3)
    assert len(page.tasks) == 3
    assert page.total_count == full.total_count == 10
    # page == full[2:5]
    assert [t["task_id"] for t in page.tasks] == [
        t["task_id"] for t in full.tasks[2:5]
    ]


def test_paginate_total_count_reflects_filter_not_window(
    engine: TaskQueryEngine,
) -> None:
    """``total_count`` is the matching-after-filter count, BEFORE
    offset+limit slicing — so the caller knows whether to fetch more.
    """
    result = engine.query(
        filters=TaskFilterSpec(status="pending"), offset=1, limit=2
    )
    assert len(result.tasks) == 2
    # 6 pending tasks in the fixture
    assert result.total_count == 6


# --- 4. Health analysis ----------------------------------------------


def test_health_of_blocked_task_reports_blocking_deps(
    engine: TaskQueryEngine, snapshot: Dict[str, Dict[str, Any]]
) -> None:
    health = engine.health_of(snapshot["t6"], snapshot)
    assert health.is_blocked is True
    # t5 (failed) and possibly t3 (completed) — only the active blockers
    # belong in blocking_dependencies; completed deps go to completed.
    assert "t5" in health.blocking_dependencies
    assert "t3" in health.completed_dependencies


def test_health_of_unblocked_task(
    engine: TaskQueryEngine, snapshot: Dict[str, Dict[str, Any]]
) -> None:
    health = engine.health_of(snapshot["t1"], snapshot)
    assert health.is_blocked is False
    assert health.can_start is True


def test_metrics_includes_blocked_and_stale_counts(
    engine: TaskQueryEngine,
) -> None:
    """``health_metrics()`` is the engine-level aggregate the handler
    surfaces as `📊 Health Analysis: ...`. Must count blocked + stale.
    """
    metrics = engine.health_metrics()
    assert metrics["total"] == 10
    # blocked = pending tasks with at least one unfinished dep
    assert metrics["blocked_tasks"] >= 1
    # stale = pending/in_progress not updated in 7+ days  (t8)
    assert metrics["stale_tasks"] >= 1
    assert 0 <= metrics["health_score"] <= 100
    assert metrics["health_status"] in {
        "excellent",
        "good",
        "needs_attention",
        "critical",
    }


# --- 5. Edge cases ---------------------------------------------------


def test_empty_repo_returns_empty_result() -> None:
    engine = TaskQueryEngine(task_source=lambda: {})
    result = engine.query()
    assert result.tasks == []
    assert result.total_count == 0


def test_snapshot_consistency_during_concurrent_write(
    snapshot: Dict[str, Dict[str, Any]],
) -> None:
    """A query takes a snapshot of the source at .query() entry.  Mid-
    iteration mutations to the underlying dict must not affect the
    result — otherwise paginated queries could return inconsistent
    pages.
    """
    engine = TaskQueryEngine(task_source=lambda: snapshot)
    result = engine.query(filters=TaskFilterSpec(status="pending"))
    pre_ids = {t["task_id"] for t in result.tasks}

    # Mutate after the query returns — the returned tasks list must be
    # an independent snapshot.
    snapshot["t1"]["status"] = "completed"
    post_ids = {t["task_id"] for t in result.tasks}
    assert pre_ids == post_ids
