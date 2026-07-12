# Agent-MCP/agent_mcp/repositories/task_repository.py
"""TaskRepository — class-based single owner of the task cache+DB invariant.

PR #146 promoted the module-of-functions ``task_repo`` from PR #137
into a real class with an instance lifecycle. Subsequent PRs
(#151, #152) migrated the call sites that previously talked directly
to ``agent_mcp.db.actions.task_db`` over to this class.

This PR (PR 7 of the architecture-review series — the "Task flip")
finishes the deepening. Until now the class delegated DB I/O to the
helpers in ``agent_mcp.db.actions.task_db`` — so even though every
handler called ``task_repo.X(...)``, the SQL still lived in
``db/actions/task_db.py``. Two layers, not one.

After the flip:

* All read/write SQL bodies live here (module-level functions for the
  legacy free-function API, instance methods for the cache-aware
  surface).
* ``agent_mcp.db.actions.task_db`` was a ~30-line re-export shim that
  kept legacy importers (the older module-of-functions repo, tests
  pinning the read-side cutover) working unchanged. arch-deepening R3
  #2b deleted the shim and repointed every importer at this module
  directly.
* ``TaskRepository`` is the single owner — handler → repo → SQL.

Public surface (class methods):

* ``delete(task_id)`` — centralised purge + cache eviction +
  ``"task.deleted"`` publish.
* ``update_fields(... , connection=)`` — Risk #1 mitigation:
  handlers that ran the legacy ``update_task_fields_in_db`` inside a
  wider transaction can pass their own open SQLAlchemy ``Session`` or
  raw ``sqlite3.Cursor`` so the migration doesn't fragment one atomic
  write into two commits.
* ``bulk_update_fields(task_ids, fields)`` — Risk #2 mitigation: 20
  separate ``update_fields`` calls in a loop produce 20 events; the
  bulk variant produces one batched event.

Module-level free functions (preserved for legacy importers that used
to go through the now-deleted ``db.actions.task_db`` shim):

* ``get_task_by_id`` / ``get_all_tasks_from_db`` / ``get_tasks_by_agent_id``
* ``update_task_fields_in_db``

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
import secrets
import sqlite3
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.exc import SQLAlchemyError

from ..core import state
from ..core.config import logger
# NOTE: we import the bus shim lazily inside the publish call sites
# below. Historically a top-level
# ``from ..core.repositories import _event_bus_shim`` would execute
# ``core.repositories.__init__``, which eagerly imported the legacy
# module-of-functions ``core.repositories.task_repo``, which in turn
# imported ``db.actions.task_db`` — a shim that re-exported from THIS
# module — producing a circular import at first load. arch-deepening
# R3 #2a deleted ``task_repo`` and #2b deleted ``db.actions.task_db``
# outright (closing that cycle for good) and relocated the bus shim to
# ``core.event_bus_shim``. Kept lazy anyway: a function-local import
# costs nothing at this call frequency.
from ..db.engine import get_session
from ..db.models import Task


def _publish(addressee: str, event: str, payload: Dict[str, Any]) -> None:
    """Lazy-import shim around ``event_bus_shim.publish``.

    See the module-level NOTE above the imports for why this stays a
    function-local import (a stale cycle this used to dodge, closed by
    arch-deepening R3 #2a/#2b).
    """
    from ..core import event_bus_shim

    event_bus_shim.publish(addressee, event, payload)


# ---------------------------------------------------------------------------
# Module-level constants — formerly lived in db/actions/task_db.py.
# Both the class methods and the free-function API below consume these.
# ---------------------------------------------------------------------------

# Columns the caller is allowed to mutate via :func:`update_task_fields_in_db`
# and :meth:`TaskRepository.update_fields`. Used both as an allowlist
# (anti-injection / anti-typo) and to centralise the JSON-serialisation
# rule for the list-typed fields.
_MUTABLE_FIELDS: set[str] = {
    "title",
    "description",
    "assigned_to",
    "status",
    "priority",
    "parent_task",
    "child_tasks",
    "depends_on_tasks",
    "notes",
}

# Subset of _MUTABLE_FIELDS that store a JSON-encoded list in their
# TEXT column. Callers pass a Python list; we json.dumps before write.
_JSON_LIST_FIELDS: set[str] = {
    "child_tasks",
    "depends_on_tasks",
    "notes",
}


def _sanitise_fields(task_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Allowlist-filter ``fields`` and JSON-encode the list-typed ones.

    The single source of truth for the ``_MUTABLE_FIELDS`` allowlist
    plus the ``_JSON_LIST_FIELDS`` json.dumps rule. Before arch-r5 #3
    this block was duplicated 3x (the standalone own-session writer,
    the shared-cursor writer, and a dead shared-session writer that no
    caller ever exercised). Both surviving writers call this so the
    invariant can't drift between them again.

    Unknown fields are dropped (with a warning), matching the legacy
    per-copy behaviour — this is an allowlist, not a validator that
    raises.
    """
    sanitised: Dict[str, Any] = {}
    for field, value in fields.items():
        if field not in _MUTABLE_FIELDS:
            logger.warning(
                f"Attempted to update invalid task field: {field} for "
                f"task {task_id}. Skipping."
            )
            continue
        if field in _JSON_LIST_FIELDS:
            sanitised[field] = json.dumps(value or [])
        else:
            sanitised[field] = value
    return sanitised


def _generate_task_id() -> str:
    """Mint an opaque, collision-resistant task id.

    arch-deepening R4 #7: this is now the ONE id-minting scheme for
    tasks. Before this PR, ``tools/task_tools.py`` carried THREE
    generators feeding ``create()`` — this opaque ``secrets``-based
    scheme (originally ``task_tools._generate_task_id``, which now
    delegates here) plus two ``f"task_{int(now().timestamp()*1000)}"``
    variants used by the unassigned/multi-create paths. The timestamp
    variants collide under concurrent same-millisecond creates
    (duplicate PK -> IntegrityError); ``secrets.token_hex`` does not.
    ``create()`` calls this whenever a caller omits ``task_id``, so
    every write path that doesn't need the id before the row exists
    gets the safe scheme for free.
    """
    return f"task_{secrets.token_hex(6)}"


# ---------------------------------------------------------------------------
# Module-level free functions — formerly lived in db/actions/task_db.py.
# These remain ORM-backed and behaviourally unchanged. Legacy callers
# (``cli.py``, tests) used to reach them via the ``db.actions.task_db``
# re-export shim; arch-deepening R3 #2b deleted that shim and
# repointed every importer at this module directly.
# ---------------------------------------------------------------------------


def _task_to_dict(row: Task) -> Dict[str, Any]:
    """Project a ``Task`` ORM row into the dict shape consumers expect.

    Mirrors the pre-cutover ``dict(sqlite_row)`` projection then
    ``_parse_task_json_fields``: every column is exposed by name, and
    the three JSON-typed columns are deserialised to Python lists.
    Parse failures fall back to an empty list with a warning (matches
    the legacy helper's behaviour exactly).

    arch-deepening R4 #7: ``required_capabilities`` used to be missing
    from this projection entirely — ``create()``'s ``connection=``
    paths always included it in the ``fresh`` dict they build by hand,
    but the standalone (no-``connection``) path re-fetches via
    :func:`get_task_by_id`, which goes through here and silently
    dropped the column. Every other reader of this projection
    (``get_by_id``, ``list_all``, ``list_by_agent``) inherited the
    same gap. Fixed here rather than left as a further landmine now
    that ``create()``'s docstring makes the returned-shape contract
    explicit.
    """
    data: Dict[str, Any] = {
        "task_id": row.task_id,
        "title": row.title,
        "description": row.description,
        "assigned_to": row.assigned_to,
        "created_by": row.created_by,
        "status": row.status,
        "priority": row.priority,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "parent_task": row.parent_task,
        "child_tasks": row.child_tasks,
        "depends_on_tasks": row.depends_on_tasks,
        "notes": row.notes,
        "required_capabilities": row.required_capabilities,
    }
    for field_key in _JSON_LIST_FIELDS:
        raw = data.get(field_key)
        if isinstance(raw, str):
            try:
                data[field_key] = json.loads(raw or "[]")
            except json.JSONDecodeError:
                logger.warning(
                    f"Failed to parse JSON for field '{field_key}' in "
                    f"task '{data.get('task_id', 'Unknown')}'. Raw: {raw}"
                )
                data[field_key] = []
        elif raw is None:
            data[field_key] = []

    # required_capabilities is NOT in _JSON_LIST_FIELDS: NULL means "no
    # requirement, anyone can claim" and must stay None, unlike the trio
    # above whose NULL defaults to [].
    raw_caps = data.get("required_capabilities")
    if isinstance(raw_caps, str):
        try:
            data["required_capabilities"] = json.loads(raw_caps)
        except json.JSONDecodeError:
            logger.warning(
                f"Failed to parse JSON for field 'required_capabilities' "
                f"in task '{data.get('task_id', 'Unknown')}'. Raw: {raw_caps}"
            )
            data["required_capabilities"] = None
    return data


def get_task_by_id(task_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single task's details from the database by task_id.

    Parses JSON fields (child_tasks, depends_on_tasks, notes) into
    Python lists. Returns None if the task is not found.
    """
    try:
        with get_session() as session:
            row = (
                session.query(Task)
                .filter(Task.task_id == task_id)
                .one_or_none()
            )
            return _task_to_dict(row) if row is not None else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching task by ID '{task_id}': {e}",
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error fetching task by ID '{task_id}': {e}",
            exc_info=True,
        )
        return None


def get_all_tasks_from_db(
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch all tasks from the database, newest first.

    Used by ``application_startup`` (populates ``g.tasks``) and by the
    ``/api/all-data`` dashboard route.

    ``limit`` (pentest R3-F3): when supplied, applies ``LIMIT`` in SQL
    so the newest ``limit`` rows are read — never a full-table
    materialise-then-slice. Callers that need a bounded read
    (``GET /api/tasks``) pass an already-clamped value; ``None``
    preserves the historical unbounded read (startup snapshot / count).
    """
    try:
        with get_session() as session:
            query = session.query(Task).order_by(Task.created_at.desc())
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()
            return [_task_to_dict(r) for r in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching all tasks: {e}", exc_info=True,
        )
        return []
    except Exception as e:
        logger.error(
            f"Unexpected error fetching all tasks: {e}", exc_info=True,
        )
        return []


def get_tasks_by_agent_id(
    agent_id: str,
    status_filter: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch tasks assigned to a specific agent, optionally filtered
    by status. Parses JSON fields for each task.

    ``limit`` (pentest R3-F3): bounds the read in SQL when supplied so
    the ``?assigned_to=`` branch of ``GET /api/tasks`` is bounded too;
    ``None`` keeps the historical unbounded read.
    """
    try:
        with get_session() as session:
            query = session.query(Task).filter(Task.assigned_to == agent_id)
            if status_filter:
                query = query.filter(Task.status == status_filter)
            query = query.order_by(Task.created_at.desc())
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()
            return [_task_to_dict(r) for r in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching tasks for agent '{agent_id}': {e}",
            exc_info=True,
        )
        return []
    except Exception as e:
        logger.error(
            f"Unexpected error fetching tasks for agent '{agent_id}': {e}",
            exc_info=True,
        )
        return []


def update_task_fields_in_db(
    task_id: str, fields_to_update: Dict[str, Any],
) -> bool:
    """Update specified fields for a task in the database.

    Always refreshes ``updated_at``. JSON-typed list fields
    (notes/child_tasks/depends_on_tasks) are serialised to TEXT.
    Returns True on success, False on any failure (unknown task,
    no valid fields, or DB error).

    The allowlist is non-negotiable — callers must not be able to
    mutate ``task_id``, ``created_by``, ``created_at``, or
    ``updated_at`` directly via this surface.
    """
    if not task_id or not fields_to_update:
        logger.warning(
            "update_task_fields_in_db called with no task_id or no "
            "fields to update."
        )
        return False

    # Pre-filter to the allowlist so the ORM ``setattr`` loop can't
    # touch anything off-limits. Mirrors the legacy
    # safe_field_mapping[field] KeyError-on-unknown guard.
    sanitised = _sanitise_fields(task_id, fields_to_update)

    if not sanitised:
        logger.info(f"No valid fields to update for task {task_id}.")
        return False

    try:
        with get_session() as session:
            row = (
                session.query(Task)
                .filter(Task.task_id == task_id)
                .one_or_none()
            )
            if row is None:
                logger.warning(
                    f"Task '{task_id}' not found or update had no "
                    f"effect in DB."
                )
                return False
            for field, value in sanitised.items():
                setattr(row, field, value)
            # Always bump updated_at, even if the caller passed one
            # (matches the legacy behaviour: updated_at was always
            # appended to the SET clause unconditionally).
            row.updated_at = datetime.datetime.now().isoformat()
            session.commit()
            logger.info(
                f"Task '{task_id}' updated in DB with fields: "
                f"{list(sanitised.keys())}."
            )
            return True
    except SQLAlchemyError as e:
        logger.error(
            f"Database error updating task '{task_id}': {e}", exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error updating task '{task_id}': {e}", exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# TaskRepository — the cache-aware seam.
# ---------------------------------------------------------------------------


class TaskRepository:
    """The class behind ``agent_mcp.repositories.task_repo``.

    Instances are cheap and stateless — every method opens a fresh
    SQLAlchemy session via ``get_session()`` (the same pattern the
    free-function helpers above use). The class identity exists so
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

    def list_all(
        self, *, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return tasks from the DB, newest first.

        DB-authoritative — ``state.tasks`` is keyed by task_id and
        doesn't preserve insertion order. Callers that need the full
        list (dashboard, startup load) read through here.

        ``limit`` (pentest R3-F3): bounds the read in SQL. The
        ``GET /api/tasks`` route passes an already-clamped value so a
        project with thousands of tasks can't materialise an unbounded
        payload; ``None`` (startup snapshot / status counts) preserves
        the historical full read.
        """
        return get_all_tasks_from_db(limit=limit)

    def list_by_agent(
        self,
        agent_id: str,
        *,
        status_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return tasks assigned to ``agent_id``, optionally filtered by status.

        DB-authoritative; same rationale as ``list_all``. Used by
        the dashboard's per-agent task panel and by future API
        endpoints that need a filtered listing. ``limit`` bounds the
        read in SQL (pentest R3-F3); ``None`` keeps the full read.
        """
        return get_tasks_by_agent_id(
            agent_id, status_filter=status_filter, limit=limit,
        )

    # --- Write interface: create ----------------------------------------

    def create(
        self,
        fields: Dict[str, Any],
        *,
        connection: Any = None,
    ) -> Dict[str, Any]:
        """INSERT a task row, update the cache, publish ``"task.created"``.

        ``fields`` is a dict carrying every column the caller wants to
        set. Required keys: ``title``, ``created_by``. Optional:
        ``task_id`` (auto-minted via :func:`_generate_task_id` when
        omitted — see arch-deepening R4 #7), ``description``,
        ``assigned_to``, ``status`` (defaults ``"pending"``),
        ``priority`` (defaults ``"medium"``), ``parent_task``,
        ``child_tasks``/``depends_on_tasks``/``notes`` (each defaults
        to ``[]`` — callers only need to pass these when the value is
        non-empty), ``required_capabilities``.

        ``connection`` is the transaction-aware seam introduced in
        PR #151 and expanded here for the multi-table writes in
        ``admin_tools`` / ``task_tools`` (task INSERTs that wrap an
        ``agent_actions`` audit-log INSERT in the same transaction).
        Tolerates EITHER a SQLAlchemy ``Session`` OR a raw
        ``sqlite3.Cursor`` so legacy hand-rolled cascades can keep
        their BEGIN/COMMIT atomic. When ``None`` (the standalone
        case), the method opens its own session.

        Returns the freshly-stored row in the same dict shape
        consumers expect (JSON list fields deserialised) — including
        the minted ``task_id`` when the caller didn't supply one, so
        callers that need the id for follow-up work (audit rows,
        parent ``child_tasks`` mirrors, cache upserts) read it off the
        return value rather than generating it themselves. On DB
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

        task_id = fields.get("task_id") or _generate_task_id()
        assigned_to = fields.get("assigned_to")
        status = fields.get("status", "pending")

        # sqlite3 cursor path: caller owns the BEGIN/COMMIT. Use raw
        # SQL so the INSERT lands in the caller's transaction.
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                """
                INSERT INTO tasks (
                    task_id, title, description, assigned_to, created_by,
                    status, priority, created_at, updated_at, parent_task,
                    child_tasks, depends_on_tasks, notes,
                    required_capabilities
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    fields["title"],
                    fields.get("description"),
                    assigned_to,
                    fields["created_by"],
                    status,
                    fields.get("priority", "medium"),
                    now,
                    now,
                    fields.get("parent_task"),
                    json.dumps(child_tasks),
                    json.dumps(depends_on_tasks),
                    json.dumps(notes),
                    json.dumps(required_caps) if required_caps else None,
                ),
            )
            # Build the dict to return + cache without re-querying —
            # the caller's transaction hasn't committed yet, so a
            # cross-session re-fetch wouldn't see the row.
            fresh = {
                "task_id": task_id,
                "title": fields["title"],
                "description": fields.get("description"),
                "assigned_to": assigned_to,
                "created_by": fields["created_by"],
                "status": status,
                "priority": fields.get("priority", "medium"),
                "created_at": now,
                "updated_at": now,
                "parent_task": fields.get("parent_task"),
                "child_tasks": child_tasks,
                "depends_on_tasks": depends_on_tasks,
                "notes": notes,
                "required_capabilities": required_caps,
            }
        elif connection is not None:
            # SQLAlchemy Session path: caller owns commit.
            session = connection
            row = Task(
                task_id=task_id,
                title=fields["title"],
                description=fields.get("description"),
                assigned_to=assigned_to,
                created_by=fields["created_by"],
                status=status,
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
            session.flush()
            fresh = {
                "task_id": task_id,
                "title": fields["title"],
                "description": fields.get("description"),
                "assigned_to": assigned_to,
                "created_by": fields["created_by"],
                "status": status,
                "priority": fields.get("priority", "medium"),
                "created_at": now,
                "updated_at": now,
                "parent_task": fields.get("parent_task"),
                "child_tasks": child_tasks,
                "depends_on_tasks": depends_on_tasks,
                "notes": notes,
                "required_capabilities": required_caps,
            }
        else:
            # Standalone path — open session + commit.
            with get_session() as session:
                row = Task(
                    task_id=task_id,
                    title=fields["title"],
                    description=fields.get("description"),
                    assigned_to=assigned_to,
                    created_by=fields["created_by"],
                    status=status,
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
            fresh = get_task_by_id(task_id)
            if fresh is None:
                # Should be unreachable; defensive fallback.
                fresh = dict(fields)

        # Cache + EventBus publish only on the standalone path. When
        # a ``connection=`` is supplied, the caller's transaction is
        # still open — a publish or cache write here could be observed
        # by a subscriber before the caller commits (or persist after
        # a rollback). The caller is responsible for calling
        # :meth:`upsert_cache` + emitting the event after its own
        # commit. The dict returned still describes the would-be row
        # so the caller can hand it straight to ``upsert_cache``.
        if connection is None:
            if not self._cache_disabled:
                state.tasks[task_id] = fresh

            _publish(
                fresh.get("assigned_to") or "*",
                "task.created",
                {
                    "task_id": task_id,
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
        connection: Optional[sqlite3.Cursor] = None,
    ) -> Optional[Dict[str, Any]]:
        """UPDATE a task via the allowlisted writer; refresh cache; publish.

        ``connection`` is the Risk #1 hook: a handler that already
        holds an open raw ``sqlite3.Cursor`` in a wider transaction
        (its own BEGIN/COMMIT) can pass it in to keep the write atomic
        with its surrounding statements. When ``None`` (the normal
        case), the call opens its own session via the existing
        ``update_task_fields_in_db`` helper.

        arch-r5 #3: a SQLAlchemy ``Session`` overload used to be
        accepted here too (disambiguated via ``hasattr(connection,
        "query")``), but a grep of every ``update_fields(...,
        connection=...)`` call site across ``agent_mcp/`` and
        ``tests/`` turned up zero Session-shaped callers — every one
        passes a raw cursor. Removed the dead branch and typed the
        parameter honestly.

        Returns the post-update dict, or ``None`` if the row was
        unknown / no valid fields supplied / DB error. Matches the
        legacy semantics so callers that today branch on a falsy
        return don't need to change.
        """
        # sqlite3 cursor path (PR #152) — caller owns BEGIN/COMMIT.
        if connection is not None:
            ok = self._update_fields_with_cursor(
                connection, task_id, fields,
            )
            if not ok:
                return None
            # Caller's transaction is still open; defer cache + publish
            # to post-commit. Return a thin dict carrying the changed
            # fields so the caller can wire them into their response.
            return {"task_id": task_id, **fields}

        ok = update_task_fields_in_db(task_id, fields)
        if not ok:
            return None

        fresh = get_task_by_id(task_id)
        if fresh is None:
            return None

        if not self._cache_disabled:
            state.tasks[task_id] = fresh

        _publish(
            fresh.get("assigned_to") or "*",
            "task.updated",
            {"task_id": task_id, "fields": list(fields.keys())},
        )
        return fresh

    def _update_fields_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        task_id: str,
        fields: Dict[str, Any],
    ) -> bool:
        """Internal helper for the sqlite3.Cursor seam path.

        Uses :func:`_sanitise_fields` — the same allowlist + JSON-
        serialisation logic the standalone ``update_task_fields_in_db``
        uses — but writes via raw SQL against the caller's cursor so
        the wider BEGIN/COMMIT stays atomic.
        """
        if not task_id or not fields:
            return False

        sanitised = _sanitise_fields(task_id, fields)
        if not sanitised:
            return False

        now = datetime.datetime.now().isoformat()
        set_parts = [f"{f} = ?" for f in sanitised.keys()]
        set_parts.append("updated_at = ?")
        sql = (
            f"UPDATE tasks SET {', '.join(set_parts)} WHERE task_id = ?"
        )
        params = list(sanitised.values()) + [now, task_id]
        try:
            cursor.execute(sql, params)
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(
                f"Database error updating task '{task_id}' via shared "
                f"cursor: {e}",
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
        _publish(
            "*",
            "task.bulk_updated",
            {
                "task_ids": updated_ids,
                "fields": list(fields.keys()),
            },
        )
        return fresh_rows

    # --- Write interface: delete ----------------------------------------

    def delete(
        self,
        task_id: str,
        *,
        connection: Any = None,
    ) -> bool:
        """DELETE a task row, evict the cache, publish ``"task.deleted"``.

        Returns True on success, False if the row didn't exist or
        the DB raised. The cache eviction happens after the DB
        commit so a failed commit doesn't desync the in-memory state.

        ``connection`` is the transaction-aware seam. Tolerates a
        SQLAlchemy ``Session`` OR a raw ``sqlite3.Cursor`` so the
        caller's wider transaction stays atomic. When ``None``, the
        method opens its own session.

        Note: this is the *simple* delete path — no cascade across
        ``agent_actions`` / ``agent_messages``. The purge-cascade in
        ``app.routes`` still owns its own multi-table SQL.
        """
        assigned_to: Optional[str] = None

        # sqlite3 cursor path: caller owns BEGIN/COMMIT.
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                "SELECT assigned_to FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            try:
                assigned_to = row["assigned_to"]
            except (KeyError, IndexError):
                assigned_to = row[0]
            cur.execute(
                "DELETE FROM tasks WHERE task_id = ?", (task_id,),
            )
        elif connection is not None:
            session = connection
            try:
                row = (
                    session.query(Task)
                    .filter(Task.task_id == task_id)
                    .one_or_none()
                )
                if row is None:
                    return False
                assigned_to = row.assigned_to
                session.delete(row)
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error deleting task '{task_id}' via "
                    f"shared session: {e}",
                    exc_info=True,
                )
                return False
        else:
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

        # As with create(connection=), defer cache + EventBus to the
        # caller when they own the transaction. Otherwise a rollback
        # would leave the cache desynced and a subscriber could see
        # "task.deleted" for a row that's still in the DB.
        if connection is None:
            if not self._cache_disabled:
                state.tasks.pop(task_id, None)

            _publish(
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


__all__ = [
    "TaskRepository",
    # Module-level free functions kept for legacy compatibility.
    # ``agent_mcp.db.actions.task_db`` used to re-export these; #2b
    # deleted that shim (importers now use this module directly).
    "get_task_by_id",
    "get_all_tasks_from_db",
    "get_tasks_by_agent_id",
    "update_task_fields_in_db",
]
