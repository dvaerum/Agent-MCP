# Agent-MCP/agent_mcp/core/repositories/task_repo.py
"""TaskRepository — owns the ``state.tasks`` cache + DB I/O for tasks.

Public contract:

Reads
    * :func:`get_task` — cache hit → return; miss → DB → warm cache.
    * :func:`list_all_tasks` — DB-authoritative listing (cache is
      key→row, not order-preserving).
    * :func:`list_tasks_assigned_to` — convenience filter; reads DB
      under the cache guard so newly-added rows show up immediately.

Writes
    * :func:`create_task` — DB insert via the ORM model, then
      update cache, then publish ``"task.created"`` to the bus.
    * :func:`update_task_fields` — delegate to
      :func:`agent_mcp.db.actions.task_db.update_task_fields_in_db`
      (the existing ORM-allowlist-protected writer), then re-fetch the
      authoritative row, update cache, publish ``"task.updated"``.
    * :func:`update_task_status` — sugar over ``update_task_fields``
      that adds the ``"task.status_changed"`` event variant.
    * :func:`delete_task_from_cache` — evict-only helper for the
      legacy purge-cascade path in ``app/routes.py``; the SQL DELETE
      is still owned by the caller because the cascade touches
      multiple tables in one transaction.

Test mode
    * :func:`disable_cache` — context manager. While active,
      ``get_*`` skips ``state.tasks`` and goes straight to DB, and
      ``create_*`` / ``update_*`` skip the cache update too. The DB
      and the bus are still touched, so the rest of the system sees
      the writes.
"""
from __future__ import annotations

import contextlib
import datetime
import json
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.exc import SQLAlchemyError

from .. import state
from ..config import logger
from ...db.actions.task_db import (
    get_all_tasks_from_db,
    get_task_by_id,
    get_tasks_by_agent_id,
    update_task_fields_in_db,
)
from ...db.engine import get_session
from ...db.models import Task
from . import _event_bus_shim


# --- test-mode flag -----------------------------------------------------

_cache_disabled: bool = False


@contextlib.contextmanager
def disable_cache() -> Iterator[None]:
    """Suspend cache reads/writes for the duration of the ``with`` block.

    Inside the block:

    * Reads skip ``state.tasks`` and go straight to DB.
    * Writes do NOT update ``state.tasks``.
    * The DB write and the bus publish still happen.

    The flag is process-wide because the legacy ``state.tasks`` cache
    is process-wide. Tests using this should not run in parallel
    against the same process (pytest-xdist gives each worker its own
    process, so the typical case is fine).
    """
    global _cache_disabled
    prev = _cache_disabled
    _cache_disabled = True
    try:
        yield
    finally:
        _cache_disabled = prev


def reset() -> None:
    """Clear the in-memory cache. For test isolation.

    Does NOT touch the DB. Equivalent to ``state.tasks.clear()`` but
    routed through the repo so the call site reads as a cache reset
    rather than an internal-state poke.
    """
    state.tasks.clear()


# --- read interface -----------------------------------------------------


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a task by id. Cache-first; falls through to DB on miss.

    Warm-on-miss: a successful DB read populates ``state.tasks`` so
    the next call is a cache hit. Cache misses are silent (DEBUG-level
    log) — they happen on cold start and after evictions.
    """
    if not _cache_disabled:
        cached = state.tasks.get(task_id)
        if cached is not None:
            return cached

    row = get_task_by_id(task_id)
    if row is not None and not _cache_disabled:
        state.tasks[task_id] = row
    return row


def list_all_tasks() -> List[Dict[str, Any]]:
    """Return every task in the DB, newest first.

    DB-authoritative — the in-memory cache is keyed by task_id and
    does not preserve insertion order. Callers that need the full
    list (dashboard /api/all-data, startup load) read through here.
    """
    return get_all_tasks_from_db()


def list_tasks_assigned_to(
    agent_id: str, *, status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return tasks whose ``assigned_to`` matches ``agent_id``.

    DB-authoritative for the same reason as :func:`list_all_tasks`.
    Used by the dashboard's per-agent task panel and the API
    endpoint ``/api/agents/<id>/tasks``.
    """
    return get_tasks_by_agent_id(agent_id, status_filter=status)


# --- write interface ----------------------------------------------------


def create_task(
    *,
    task_id: str,
    title: str,
    description: Optional[str] = None,
    assigned_to: Optional[str] = None,
    created_by: str,
    status: str = "pending",
    priority: str = "medium",
    parent_task: Optional[str] = None,
    child_tasks: Optional[List[Any]] = None,
    depends_on_tasks: Optional[List[Any]] = None,
    notes: Optional[List[Any]] = None,
    required_capabilities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """INSERT a task row, update cache, publish ``"task.created"``.

    Returns the dict shape consumers expect (same projection as
    :func:`get_task_by_id`). On DB failure, raises the underlying
    :class:`SQLAlchemyError`; the cache and bus are NOT touched.
    """
    now = datetime.datetime.now().isoformat()
    child_tasks_json = json.dumps(child_tasks or [])
    depends_on_tasks_json = json.dumps(depends_on_tasks or [])
    notes_json = json.dumps(notes or [])
    required_caps_json = (
        json.dumps(required_capabilities)
        if required_capabilities
        else None
    )

    with get_session() as session:
        row = Task(
            task_id=task_id,
            title=title,
            description=description,
            assigned_to=assigned_to,
            created_by=created_by,
            status=status,
            priority=priority,
            created_at=now,
            updated_at=now,
            parent_task=parent_task,
            child_tasks=child_tasks_json,
            depends_on_tasks=depends_on_tasks_json,
            notes=notes_json,
            required_capabilities=required_caps_json,
        )
        session.add(row)
        session.commit()

    # Re-fetch via the existing dict projection so consumers see the
    # exact shape they're used to (JSON fields deserialised, etc.).
    fresh = get_task_by_id(task_id)
    if fresh is None:
        # Should be unreachable; defensive fallback.
        fresh = {
            "task_id": task_id,
            "title": title,
            "description": description,
            "assigned_to": assigned_to,
            "created_by": created_by,
            "status": status,
            "priority": priority,
            "created_at": now,
            "updated_at": now,
            "parent_task": parent_task,
            "child_tasks": child_tasks or [],
            "depends_on_tasks": depends_on_tasks or [],
            "notes": notes or [],
        }

    if not _cache_disabled:
        state.tasks[task_id] = fresh

    _event_bus_shim.publish(
        assigned_to or "*",
        "task.created",
        {"task_id": task_id, "status": status, "assigned_to": assigned_to},
    )
    return fresh


def update_task_fields(
    task_id: str, fields: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """UPDATE a task via the ORM-allowlist writer, refresh cache, publish.

    Returns the post-update dict, or ``None`` if the underlying
    update was a no-op (unknown task, no valid fields, DB error).
    """
    ok = update_task_fields_in_db(task_id, fields)
    if not ok:
        return None

    fresh = get_task_by_id(task_id)
    if fresh is None:
        return None

    if not _cache_disabled:
        state.tasks[task_id] = fresh

    _event_bus_shim.publish(
        fresh.get("assigned_to") or "*",
        "task.updated",
        {"task_id": task_id, "fields": list(fields.keys())},
    )
    return fresh


def update_task_status(
    task_id: str,
    new_status: str,
    *,
    updated_by: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """UPDATE a task's status, refresh cache, publish ``"task.status_changed"``.

    Thin sugar over :func:`update_task_fields` that picks a more
    specific event type so consumers wiring to ``"task.status_changed"``
    don't have to filter the generic ``"task.updated"`` stream.
    """
    ok = update_task_fields_in_db(task_id, {"status": new_status})
    if not ok:
        return None

    fresh = get_task_by_id(task_id)
    if fresh is None:
        return None

    if not _cache_disabled:
        state.tasks[task_id] = fresh

    _event_bus_shim.publish(
        fresh.get("assigned_to") or "*",
        "task.status_changed",
        {
            "task_id": task_id,
            "new_status": new_status,
            "updated_by": updated_by,
        },
    )
    return fresh


def upsert_cache(row: Dict[str, Any]) -> None:
    """Insert/overwrite a single cache entry without going through the DB.

    Used by the legacy tool surfaces that own their own raw SQL
    INSERT in a wider transaction (e.g.
    ``task_tools.create_self_task``) and still need to keep the
    cache in sync. Once those surfaces migrate to
    :func:`create_task`, this helper can be deleted.
    """
    if _cache_disabled:
        return
    task_id = row.get("task_id")
    if not task_id:
        logger.warning(
            "task_repo.upsert_cache called without task_id; ignoring"
        )
        return
    state.tasks[task_id] = row


def delete_task_from_cache(task_id: str) -> None:
    """Evict a task from the in-memory cache.

    The SQL DELETE is still owned by the caller because the legacy
    purge-cascade in :mod:`agent_mcp.app.routes` deletes the row
    alongside agent_actions / agent_messages / task_notes in one
    transaction. This helper exists so the routes can replace
    ``del state.tasks[task_id]`` with a named call without changing
    the cascade semantics.
    """
    if _cache_disabled:
        return
    state.tasks.pop(task_id, None)


# Re-export for type-checkers / callers that want the raw DB error
# class without importing sqlalchemy themselves.
__all__ = [
    "create_task",
    "delete_task_from_cache",
    "disable_cache",
    "get_task",
    "list_all_tasks",
    "list_tasks_assigned_to",
    "reset",
    "SQLAlchemyError",
    "update_task_fields",
    "update_task_status",
    "upsert_cache",
]
