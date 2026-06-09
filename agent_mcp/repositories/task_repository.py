# Agent-MCP/agent_mcp/repositories/task_repository.py
"""TaskRepository — class-based single owner of the task cache+DB invariant.

PR #146 promotes the module-of-functions ``task_repo`` from PR #137
into a real class with an instance lifecycle. The behavior on
existing methods is preserved verbatim (return shapes, JSON-list
deserialisation, ``state.tasks`` write-through, EventBus publish);
the class form adds:

* **``delete(task_id)``** — the legacy purge path in ``app.routes``
  owned its own SQL DELETE and ad-hoc ``del state.tasks[task_id]``
  follow-up. ``delete`` centralises both, plus the
  ``"task.deleted"`` publish, so the cache invariant is owned in
  exactly one place.

* **``update_fields(... , connection=)``** — Risk #1 mitigation
  from the grilling: handlers that ran the legacy
  ``update_task_fields_in_db`` inside a wider transaction can pass
  their own open SQLAlchemy ``Session`` so the migration doesn't
  fragment one atomic write into two commits. The class signature
  carries this even though the current call sites don't need it —
  it's a documented affordance for future migrations.

* **``bulk_update_fields(task_ids, fields)``** — Risk #2 mitigation:
  20 separate ``update_fields`` calls in a loop produce 20 events;
  the bulk variant produces one batched event so subscribers don't
  drown in per-row noise.

The class delegates DB I/O to the existing helpers in
``agent_mcp.db.actions.task_db`` and the SQLAlchemy ORM layer
behind them — no SQL gets re-written here. The class is the *seam*
between business logic and persistence, not a re-implementation of
either.

Event types published (subscribers can route by exact string):

* ``"task.created"`` — emitted by ``create`` on success.
* ``"task.updated"`` — emitted by ``update_fields`` on success.
* ``"task.bulk_updated"`` — emitted once per ``bulk_update_fields``
  call, regardless of row count.
* ``"task.deleted"`` — emitted by ``delete`` on success.

All four events go to the addressee ``assigned_to or "*"`` —
matching the convention the existing module-of-functions ``task_repo``
established for ``"task.created"`` and ``"task.updated"``.
"""
from __future__ import annotations

import contextlib
import datetime
import json
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.exc import SQLAlchemyError

from ..core import state
from ..core.config import logger
from ..core.repositories import _event_bus_shim
from ..db.actions.task_db import (
    _MUTABLE_FIELDS,
    _JSON_LIST_FIELDS,
    get_all_tasks_from_db,
    get_task_by_id,
    get_tasks_by_agent_id,
    update_task_fields_in_db,
)
from ..db.engine import get_session
from ..db.models import Task


class TaskRepository:
    """The class behind ``agent_mcp.repositories.task_repo``.

    Instances are cheap and stateless — every method opens a fresh
    SQLAlchemy session via ``get_session()`` (the same pattern the
    existing ``task_db`` helpers use). The class identity exists so
    callers can hold a reference, type-check against
    ``TaskRepository``, and (in future PRs) attach per-instance
    state like batching policies or audit hooks without rewriting
    every call site.

    The class is the **single owner** of the cache+DB invariant for
    tasks:

    1. Reads consult ``state.tasks`` first, fall through to the DB
       on miss, warm the cache on the way back.
    2. Writes touch the DB first, then update ``state.tasks``, then
       publish to the EventBus. The order matters: a write that
       fails at step 1 must not invalidate the cache, and a publish
       that fires before the cache update could race with a
       subscriber that immediately re-reads the cache.
    """

    # --- Test-mode flag --------------------------------------------------
    #
    # Mirrors the module-level flag on the legacy ``core.repositories.
    # task_repo``. Tests that exercise DB-only behaviour can enter
    # ``disable_cache()`` to suspend ``state.tasks`` interaction for the
    # duration of a ``with`` block.

    _cache_disabled: bool = False

    @contextlib.contextmanager
    def disable_cache(self) -> Iterator[None]:
        """Suspend cache reads/writes inside the ``with`` block.

        DB writes and EventBus publishes still happen — only the
        ``state.tasks`` interaction is skipped. Useful for tests that
        want to verify DB-only behaviour without dealing with cache
        invariants.
        """
        prev = self._cache_disabled
        self._cache_disabled = True
        try:
            yield
        finally:
            self._cache_disabled = prev

    # --- Read interface --------------------------------------------------

    def get_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a task by id. Cache-first; falls through to DB on miss.

        Warm-on-miss: a successful DB read populates ``state.tasks``
        so the next call is a cache hit. Cache misses are silent
        (the underlying DB helper logs at DEBUG/WARN as needed).
        """
        if not self._cache_disabled:
            cached = state.tasks.get(task_id)
            if cached is not None:
                return cached

        row = get_task_by_id(task_id)
        if row is not None and not self._cache_disabled:
            state.tasks[task_id] = row
        return row

    def list_all(self) -> List[Dict[str, Any]]:
        """Return every task in the DB, newest first.

        DB-authoritative — ``state.tasks`` is keyed by task_id and
        doesn't preserve insertion order. Callers that need the full
        list (dashboard, startup load) read through here.
        """
        return get_all_tasks_from_db()

    def list_by_agent(
        self,
        agent_id: str,
        *,
        status_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return tasks assigned to ``agent_id``, optionally filtered by status.

        DB-authoritative; same rationale as ``list_all``. Used by
        the dashboard's per-agent task panel and by future API
        endpoints that need a filtered listing.
        """
        return get_tasks_by_agent_id(agent_id, status_filter=status_filter)

    # --- Write interface: create ----------------------------------------

    def create(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """INSERT a task row, update the cache, publish ``"task.created"``.

        ``fields`` is a dict carrying every column the caller wants to
        set. Required keys: ``task_id``, ``title``, ``created_by``.
        Optional: ``description``, ``assigned_to``, ``status``,
        ``priority``, ``parent_task``, ``child_tasks``,
        ``depends_on_tasks``, ``notes``, ``required_capabilities``.

        Returns the freshly-stored row in the same dict shape
        consumers expect (JSON list fields deserialised). On DB
        conflict (e.g. duplicate ``task_id``), raises the underlying
        ``SQLAlchemyError`` — silently returning the existing row
        would mask write conflicts and the legacy raw-SQL path
        raised IntegrityError too.
        """
        now = datetime.datetime.now().isoformat()
        child_tasks = fields.get("child_tasks") or []
        depends_on_tasks = fields.get("depends_on_tasks") or []
        notes = fields.get("notes") or []
        required_caps = fields.get("required_capabilities")

        with get_session() as session:
            row = Task(
                task_id=fields["task_id"],
                title=fields["title"],
                description=fields.get("description"),
                assigned_to=fields.get("assigned_to"),
                created_by=fields["created_by"],
                status=fields.get("status", "pending"),
                priority=fields.get("priority", "medium"),
                created_at=now,
                updated_at=now,
                parent_task=fields.get("parent_task"),
                child_tasks=json.dumps(child_tasks),
                depends_on_tasks=json.dumps(depends_on_tasks),
                notes=json.dumps(notes),
                required_capabilities=(
                    json.dumps(required_caps) if required_caps else None
                ),
            )
            session.add(row)
            session.commit()

        # Re-fetch via the existing dict projection so consumers see
        # the exact shape they're used to (JSON fields deserialised).
        fresh = get_task_by_id(fields["task_id"])
        if fresh is None:
            # Should be unreachable; defensive fallback mirrors the
            # legacy module-of-functions implementation.
            fresh = dict(fields)

        if not self._cache_disabled:
            state.tasks[fields["task_id"]] = fresh

        _event_bus_shim.publish(
            fresh.get("assigned_to") or "*",
            "task.created",
            {
                "task_id": fields["task_id"],
                "status": fresh.get("status"),
                "assigned_to": fresh.get("assigned_to"),
            },
        )
        return fresh

    # --- Write interface: update ----------------------------------------

    def update_fields(
        self,
        task_id: str,
        fields: Dict[str, Any],
        *,
        connection: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """UPDATE a task via the allowlisted writer; refresh cache; publish.

        ``connection`` is the Risk #1 hook: a handler that already
        holds an open SQLAlchemy ``Session`` (or future ``Connection``)
        in a wider transaction can pass it in to keep the write
        atomic with its surrounding statements. When ``None`` (the
        normal case), the call opens its own session via the existing
        ``update_task_fields_in_db`` helper.

        Returns the post-update dict, or ``None`` if the row was
        unknown / no valid fields supplied / DB error. Matches the
        legacy semantics so callers that today branch on a falsy
        return don't need to change.
        """
        if connection is not None:
            ok = self._update_fields_with_session(
                connection, task_id, fields,
            )
        else:
            ok = update_task_fields_in_db(task_id, fields)
        if not ok:
            return None

        fresh = get_task_by_id(task_id)
        if fresh is None:
            return None

        if not self._cache_disabled:
            state.tasks[task_id] = fresh

        _event_bus_shim.publish(
            fresh.get("assigned_to") or "*",
            "task.updated",
            {"task_id": task_id, "fields": list(fields.keys())},
        )
        return fresh

    def _update_fields_with_session(
        self,
        session: Any,
        task_id: str,
        fields: Dict[str, Any],
    ) -> bool:
        """Internal helper for the ``connection=`` overload.

        Mirrors the same allowlist + JSON-serialisation logic the
        standalone ``update_task_fields_in_db`` does, but against
        the caller-provided session so the wider transaction stays
        intact. Kept private because the public API is
        ``update_fields(task_id, fields, connection=)`` — exposing
        the session-shaped variant directly would leak a transient
        implementation detail.
        """
        if not task_id or not fields:
            return False

        sanitised: Dict[str, Any] = {}
        for field, value in fields.items():
            if field not in _MUTABLE_FIELDS:
                logger.warning(
                    f"Attempted to update invalid task field: {field} "
                    f"for task {task_id}. Skipping."
                )
                continue
            if field in _JSON_LIST_FIELDS:
                sanitised[field] = json.dumps(value or [])
            else:
                sanitised[field] = value

        if not sanitised:
            return False

        try:
            row = (
                session.query(Task)
                .filter(Task.task_id == task_id)
                .one_or_none()
            )
            if row is None:
                return False
            for field, value in sanitised.items():
                setattr(row, field, value)
            row.updated_at = datetime.datetime.now().isoformat()
            session.flush()
            return True
        except SQLAlchemyError as e:
            logger.error(
                f"Database error updating task '{task_id}' via shared "
                f"session: {e}",
                exc_info=True,
            )
            return False

    # --- Write interface: bulk update -----------------------------------

    def bulk_update_fields(
        self,
        task_ids: List[str],
        fields: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """UPDATE many tasks with the same field set; publish one event.

        Risk #2 mitigation from grilling: code today that does
        ``for tid in tids: update_task_fields_in_db(tid, {...})``
        produces N publishes — one per row. ``bulk_update_fields``
        does the same N row updates inside a single session and
        emits exactly one ``"task.bulk_updated"`` event carrying
        the list of affected ids.

        Returns the list of post-update rows (in input order, with
        unknown ids silently skipped). Empty input or zero-effect
        update yields ``[]`` and no event — matches the legacy
        no-op semantics.
        """
        if not task_ids or not fields:
            return []

        sanitised: Dict[str, Any] = {}
        for field, value in fields.items():
            if field not in _MUTABLE_FIELDS:
                logger.warning(
                    f"Attempted bulk update with invalid task field: "
                    f"{field}. Skipping."
                )
                continue
            if field in _JSON_LIST_FIELDS:
                sanitised[field] = json.dumps(value or [])
            else:
                sanitised[field] = value

        if not sanitised:
            return []

        updated_ids: List[str] = []
        now = datetime.datetime.now().isoformat()
        try:
            with get_session() as session:
                for tid in task_ids:
                    row = (
                        session.query(Task)
                        .filter(Task.task_id == tid)
                        .one_or_none()
                    )
                    if row is None:
                        continue
                    for field, value in sanitised.items():
                        setattr(row, field, value)
                    row.updated_at = now
                    updated_ids.append(tid)
                session.commit()
        except SQLAlchemyError as e:
            logger.error(
                f"Database error in bulk_update_fields: {e}",
                exc_info=True,
            )
            return []

        if not updated_ids:
            return []

        # Re-fetch + cache update happens after commit so the cache
        # reflects only successfully-persisted rows.
        fresh_rows: List[Dict[str, Any]] = []
        for tid in updated_ids:
            row = get_task_by_id(tid)
            if row is None:
                continue
            if not self._cache_disabled:
                state.tasks[tid] = row
            fresh_rows.append(row)

        # Single batched publish — no per-row events. Addressee is "*"
        # because bulk updates almost always span multiple agents (or
        # touch unassigned rows).
        _event_bus_shim.publish(
            "*",
            "task.bulk_updated",
            {
                "task_ids": updated_ids,
                "fields": list(fields.keys()),
            },
        )
        return fresh_rows

    # --- Write interface: delete ----------------------------------------

    def delete(self, task_id: str) -> bool:
        """DELETE a task row, evict the cache, publish ``"task.deleted"``.

        Returns True on success, False if the row didn't exist or
        the DB raised. The cache eviction happens after the DB
        commit so a failed commit doesn't desync the in-memory state.

        Note: this is the *simple* delete path — no cascade. The
        legacy purge-cascade in ``app.routes`` (which deletes
        ``agent_actions`` / ``agent_messages`` / ``task_notes``
        rows in the same transaction) still owns its own SQL. Once
        a follow-up PR untangles that cascade, the routes will be
        able to call ``task_repo.delete(task_id)`` for each row
        without losing transactional atomicity (or — preferred — the
        cascade moves into this method behind a flag).
        """
        try:
            with get_session() as session:
                row = (
                    session.query(Task)
                    .filter(Task.task_id == task_id)
                    .one_or_none()
                )
                if row is None:
                    return False
                assigned_to = row.assigned_to
                session.delete(row)
                session.commit()
        except SQLAlchemyError as e:
            logger.error(
                f"Database error deleting task '{task_id}': {e}",
                exc_info=True,
            )
            return False

        if not self._cache_disabled:
            state.tasks.pop(task_id, None)

        _event_bus_shim.publish(
            assigned_to or "*",
            "task.deleted",
            {"task_id": task_id, "assigned_to": assigned_to},
        )
        return True

    # --- Cache-only helpers ---------------------------------------------
    #
    # These exist so the legacy raw-SQL call sites in ``task_tools.py`` and
    # ``app.routes`` can keep their wider transactions intact while still
    # routing cache mutation through the repository. Once the transactional
    # surfaces migrate to ``create`` / ``update_fields`` / ``delete``
    # proper, these can be removed.

    def upsert_cache(self, row: Dict[str, Any]) -> None:
        """Insert/overwrite a single cache entry without going through the DB.

        Used by tool surfaces that own their own raw SQL INSERT
        inside a wider transaction (e.g. assign_task's multi-row
        path) and still need to keep ``state.tasks`` in sync.
        """
        if self._cache_disabled:
            return
        task_id = row.get("task_id")
        if not task_id:
            logger.warning(
                "task_repo.upsert_cache called without task_id; ignoring"
            )
            return
        state.tasks[task_id] = row

    def evict_from_cache(self, task_id: str) -> None:
        """Drop a single cache entry without going through the DB.

        Used by the legacy purge-cascade path that owns its own SQL
        DELETE. Once the cascade migrates to ``delete``, this can be
        removed.
        """
        if self._cache_disabled:
            return
        state.tasks.pop(task_id, None)


__all__ = ["TaskRepository"]
