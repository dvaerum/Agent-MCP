"""TaskQueryEngine — the business rules for task listing.

This module is the **engine layer** between the MCP handler
(``view_tasks_tool_impl``) and the repository / global task store.
It owns the rules — filter, sort, pagination, dependency analysis,
health metrics — so they're unit-testable without the LLM-text
adapter the handler returns.

Architecture sketch::

    Repository (SQL)
        |
        v
    g.tasks  (Dict[task_id, row])
        |
        v   <-- task_source callable
    TaskQueryEngine.query(filters, sort, offset, limit) -> QueryResult
        |
        v
    view_tasks_tool_impl (handler)  -- presentation: emoji, token
                                       budget, formatting, tips.

The engine ``builds on`` the repository — it does NOT issue SQL. The
caller hands it ``task_source`` (``lambda: g.tasks`` in production, a
dict in tests).

Snapshot semantics: ``query()`` calls ``task_source()`` once and
deep-copies the returned tasks so subsequent mutations to the
underlying store do not race-mutate already-returned results.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# --- Public dataclasses -----------------------------------------------


@dataclass(frozen=True)
class TaskFilterSpec:
    """Declarative filter rules.

    All fields are optional; ``None`` (or False for boolean flags)
    means "do not filter on this dimension".
    """

    status: Optional[str] = None
    priority: Optional[str] = None
    agent_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    blocked_only: bool = False


@dataclass(frozen=True)
class TaskSortSpec:
    """Sort rule.

    Supported keys: ``"created_at"`` (default), ``"updated_at"``,
    ``"priority"``, ``"status"``. All four sort *descending* — that's
    the legacy handler default and the test contract.
    """

    by: str = "created_at"


@dataclass
class TaskHealth:
    """Per-task dependency analysis result."""

    is_blocked: bool = False
    can_start: bool = True
    blocking_dependencies: List[str] = field(default_factory=list)
    completed_dependencies: List[str] = field(default_factory=list)
    missing_dependencies: List[str] = field(default_factory=list)
    blocks_tasks: List[str] = field(default_factory=list)
    dependency_health: str = "healthy"

    def as_dict(self) -> Dict[str, Any]:
        """Render in the legacy ``_analyze_task_dependencies`` shape.

        The handler's ``_format_task_with_dependencies`` reads these
        exact keys, so the engine emits them unchanged for the
        adapter.
        """
        return {
            "is_blocked": self.is_blocked,
            "can_start": self.can_start,
            "blocking_dependencies": list(self.blocking_dependencies),
            "completed_dependencies": list(self.completed_dependencies),
            "missing_dependencies": list(self.missing_dependencies),
            "blocks_tasks": list(self.blocks_tasks),
            "dependency_health": self.dependency_health,
        }


@dataclass
class QueryResult:
    """Result of a ``TaskQueryEngine.query()`` call.

    ``tasks`` is the *window* after offset+limit slicing.
    ``total_count`` is the matching-after-filter count BEFORE the
    window — so the caller can render "Total: N" / "showing M of N".
    """

    tasks: List[Dict[str, Any]]
    total_count: int


# --- Constants for the rules (kept module-local on purpose) -----------

_PRIORITY_ORDER = {"high": 3, "medium": 2, "low": 1}
_STATUS_ORDER = {
    "failed": 5,
    "in_progress": 4,
    "pending": 3,
    "completed": 2,
    "cancelled": 1,
}
_REVERSE_SORT_KEYS = {"created_at", "updated_at", "priority", "status"}
_STALE_DAYS = 7
_ACTIVE_STATUSES = {"in_progress", "pending"}


# --- Engine -----------------------------------------------------------


class TaskQueryEngine:
    """Filter / sort / paginate / analyze tasks.

    Parameters
    ----------
    task_source:
        Zero-arg callable returning the current
        ``Dict[task_id, task_row]`` view. In production this is
        ``lambda: g.tasks``; in tests it's the fixture dict.
    now:
        Optional callable returning a ``datetime`` — injected so the
        stale-task rule is deterministic in tests. Defaults to
        ``datetime.datetime.now``.
    """

    def __init__(
        self,
        task_source: Callable[[], Dict[str, Dict[str, Any]]],
        *,
        now: Optional[Callable[[], _dt.datetime]] = None,
    ) -> None:
        self._task_source = task_source
        self._now = now or _dt.datetime.now

    # -- snapshot / dependency helpers ---------------------------------

    def _snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Deep-copy the current task source.

        Deep-copy so callers iterating the result are insulated from
        in-place row mutations by concurrent writers.
        """
        return copy.deepcopy(self._task_source())

    @staticmethod
    def _coerce_deps(value: Any) -> List[str]:
        """Normalize ``depends_on_tasks`` — DB rows store JSON-text."""
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return list(parsed)
            except Exception:
                return []
        return []

    def health_of(
        self,
        task: Dict[str, Any],
        all_tasks: Dict[str, Dict[str, Any]],
    ) -> TaskHealth:
        """Compute the dependency analysis for a single task.

        Mirrors the legacy ``_analyze_task_dependencies`` rules.
        """
        task_id = task.get("task_id")
        status = task.get("status")
        depends_on = self._coerce_deps(task.get("depends_on_tasks", []))

        h = TaskHealth()

        for dep_id in depends_on:
            if dep_id in all_tasks:
                dep_status = all_tasks[dep_id].get("status")
                if dep_status == "completed":
                    h.completed_dependencies.append(dep_id)
                elif dep_status in ("failed", "cancelled"):
                    h.blocking_dependencies.append(dep_id)
                    h.is_blocked = True
                    h.can_start = False
                elif dep_status in ("pending", "in_progress"):
                    h.blocking_dependencies.append(dep_id)
                    if status == "pending":
                        h.can_start = False
            else:
                h.missing_dependencies.append(dep_id)
                h.is_blocked = True
                h.can_start = False

        # Reverse index: which tasks depend on this one
        for other_id, other_task in all_tasks.items():
            other_deps = self._coerce_deps(other_task.get("depends_on_tasks", []))
            if task_id in other_deps:
                h.blocks_tasks.append(other_id)

        # Roll up to a coarse grade for the adapter
        if h.missing_dependencies:
            h.dependency_health = "critical"
        elif h.is_blocked and status == "in_progress":
            h.dependency_health = "warning"
        elif not h.can_start and status == "pending":
            h.dependency_health = "waiting"

        return h

    # -- the rules: filter, sort, paginate -----------------------------

    @staticmethod
    def _matches(
        task: Dict[str, Any],
        filters: TaskFilterSpec,
        all_tasks: Dict[str, Dict[str, Any]],
        blocked_evaluator: Callable[[Dict[str, Any]], bool],
    ) -> bool:
        if filters.status and task.get("status") != filters.status:
            return False
        if filters.priority and task.get("priority") != filters.priority:
            return False
        if (
            filters.agent_id
            and task.get("assigned_to") != filters.agent_id
        ):
            return False
        if (
            filters.parent_task_id
            and task.get("parent_task") != filters.parent_task_id
        ):
            return False
        if filters.blocked_only and not blocked_evaluator(task):
            return False
        return True

    @staticmethod
    def _sort_key(task: Dict[str, Any], sort: TaskSortSpec) -> Any:
        if sort.by == "priority":
            return _PRIORITY_ORDER.get(task.get("priority", "medium"), 2)
        if sort.by == "status":
            return _STATUS_ORDER.get(task.get("status", "pending"), 3)
        if sort.by == "updated_at":
            return task.get("updated_at", "")
        # default: created_at
        return task.get("created_at", "")

    def query(
        self,
        *,
        filters: Optional[TaskFilterSpec] = None,
        sort: Optional[TaskSortSpec] = None,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Run filter -> sort -> paginate against the current snapshot.

        Returns the windowed slice plus ``total_count`` (matching count
        BEFORE the window) so the caller can render pagination hints.
        """
        filters = filters or TaskFilterSpec()
        sort = sort or TaskSortSpec()

        snapshot = self._snapshot()

        def is_blocked(task: Dict[str, Any]) -> bool:
            h = self.health_of(task, snapshot)
            return h.is_blocked or not h.can_start

        matched: List[Dict[str, Any]] = [
            row
            for row in snapshot.values()
            if self._matches(row, filters, snapshot, is_blocked)
        ]

        matched.sort(
            key=lambda t: self._sort_key(t, sort),
            reverse=sort.by in _REVERSE_SORT_KEYS,
        )

        total_count = len(matched)
        if offset:
            matched = matched[offset:]
        if limit is not None:
            matched = matched[:limit]

        return QueryResult(tasks=matched, total_count=total_count)

    # -- aggregate metrics --------------------------------------------

    def health_metrics(
        self,
        tasks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Aggregate metrics over a task set.

        When ``tasks`` is None, runs over the current snapshot (used by
        the engine-level health dashboard). When ``tasks`` is provided,
        runs over that explicit list (used by the handler to report
        metrics on the filtered window — matches legacy behavior).
        """
        if tasks is None:
            tasks = list(self._snapshot().values())

        if not tasks:
            return {"total": 0, "status": "no_data"}

        total = len(tasks)
        status_counts: Dict[str, int] = {}
        priority_counts: Dict[str, int] = {}
        blocked_count = 0
        stale_count = 0

        current_time = self._now()

        for task in tasks:
            status = task.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

            priority = task.get("priority", "medium")
            priority_counts[priority] = priority_counts.get(priority, 0) + 1

            deps = self._coerce_deps(task.get("depends_on_tasks", []))
            if deps and status == "pending":
                blocked_count += 1

            updated_at = task.get("updated_at")
            if updated_at:
                try:
                    updated_time = _dt.datetime.fromisoformat(
                        updated_at.replace("Z", "+00:00").replace("+00:00", "")
                    )
                    days_since_update = (current_time - updated_time).days
                    if (
                        days_since_update > _STALE_DAYS
                        and status in _ACTIVE_STATUSES
                    ):
                        stale_count += 1
                except Exception:
                    pass

        completed_ratio = status_counts.get("completed", 0) / total
        active_ratio = (
            status_counts.get("in_progress", 0)
            + status_counts.get("pending", 0)
        ) / total
        blocked_ratio = blocked_count / total if total > 0 else 0
        stale_ratio = stale_count / total if total > 0 else 0

        health_score = max(
            0,
            min(
                100,
                completed_ratio * 30
                + active_ratio * 40
                + (1 - blocked_ratio) * 20
                + (1 - stale_ratio) * 10,
            ),
        )

        if health_score >= 80:
            health_status = "excellent"
        elif health_score >= 60:
            health_status = "good"
        elif health_score >= 40:
            health_status = "needs_attention"
        else:
            health_status = "critical"

        return {
            "total": total,
            "status_distribution": status_counts,
            "priority_distribution": priority_counts,
            "blocked_tasks": blocked_count,
            "stale_tasks": stale_count,
            "health_score": round(health_score, 1),
            "health_status": health_status,
        }


__all__ = [
    "QueryResult",
    "TaskFilterSpec",
    "TaskHealth",
    "TaskQueryEngine",
    "TaskSortSpec",
]
