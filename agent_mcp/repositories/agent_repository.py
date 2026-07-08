# Agent-MCP/agent_mcp/repositories/agent_repository.py
"""AgentRepository — class-based single owner of the agent cache+DB invariant.

PR #146 established the class-based Repository pattern for tasks.
This module clones the pattern for the Agent concept. The behaviour
on existing methods preserves the legacy module-of-functions surface
under :mod:`agent_mcp.core.repositories.agent_repo` verbatim (return
shapes, capabilities JSON deserialisation, dual-cache write-through,
EventBus publish); the class form adds:

* **``terminate(agent_id)``** — the legacy
  :func:`agent_mcp.tools.admin_tools.terminate_agent` owns its own SQL
  UPDATE + raw cache eviction. ``terminate`` centralises the
  "status='terminated', terminated_at=now, evict both caches, publish
  ``agent.terminated``" ritual so the cache invariant is owned in
  exactly one place — even when the admin handler retains its outer
  transaction (it does, because it also writes to ``agent_actions``).

* **``update_field(... , connection=)``** — Risk #1 hook mirrored
  from ``TaskRepository.update_fields``: handlers that already hold
  an open SQLAlchemy ``Session`` in a wider transaction can pass it
  in to keep the write atomic with the surrounding statements. None
  of the audited admin_tools call sites need this today, but the
  affordance is part of the documented Repository contract so a
  future PR migrating those multi-table writes doesn't have to
  re-engineer the seam.

The class is the **single owner** of TWO caches because the legacy
``state.active_agents`` is keyed by **token** (the bearer secret used
for auth on every MCP call) while ``state.agent_working_dirs`` is
keyed by **agent_id** (the human-readable name). Both are views over
the same agent row; the PR #130 bug was caused by these falling out
of step with the DB. Centralising both behind one writer is the whole
point of the repo seam.

Event types published (subscribers can route by exact string):

* ``"agent.created"`` — emitted by ``create`` on success.
* ``"agent.status_changed"`` — emitted by ``update_field`` when the
  field is ``status``.
* ``"agent.updated"`` — emitted by ``update_field`` for every other
  field. Matches the convention the module-of-functions
  :mod:`core.repositories.agent_repo` established.
* ``"agent.terminated"`` — emitted by ``terminate`` on success.

PR 8 of the architecture-review series — the "Agent flip". Until this
PR the class delegated DB I/O to the helpers in
``agent_mcp.db.actions.agent_db``: handler → repo → db/actions →
SQL. Two layers, not one — the "single ownership" the class claims in
its docstring did not match the code.

After the flip:

* All read/write SQL bodies live here (module-level functions for the
  legacy free-function API, instance methods for the cache-aware
  surface).
* ``agent_mcp.db.actions.agent_db`` is a re-export shim that keeps
  legacy importers (``cli.py``, ``core.auth``, ``app.routes`` lifespan,
  the older module-of-functions repo under ``core.repositories``,
  ``tests/test_sqlalchemy_agent.py``) working unchanged.
* ``AgentRepository`` is the single owner — handler → repo → SQL.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError

from ..core import state
from ..core.config import logger
# NOTE: we import the bus shim lazily inside the publish call sites
# below. A top-level ``from ..core.repositories import _event_bus_shim``
# would execute ``core.repositories.__init__``, which eagerly imports
# the legacy module-of-functions ``core.repositories.agent_repo``,
# which in turn imports ``db.actions.agent_db`` — now a shim that
# re-exports from THIS module. That produces a circular import at
# first load. The lazy import inside ``_publish`` breaks the cycle
# without changing publish semantics (the shim is itself lazy-imports
# event_bus on every call, so call-site latency is unaffected).
from ..db.engine import get_session
from ..db.models import Agent


def _publish(addressee: str, event: str, payload: Dict[str, Any]) -> None:
    """Lazy-import shim around ``_event_bus_shim.publish``.

    Importing the submodule path directly under ``core.repositories``
    via ``from ... import _event_bus_shim`` would still trigger
    ``core.repositories.__init__`` (which eagerly imports the legacy
    ``agent_repo`` module-of-functions, which imports the shim at
    ``db.actions.agent_db``, which now re-exports from THIS module —
    circular). Doing the import at call time keeps the publish
    side-effect identical while avoiding the cycle.
    """
    from ..core.repositories import _event_bus_shim

    _event_bus_shim.publish(addressee, event, payload)


# ---------------------------------------------------------------------------
# Module-level constants — formerly lived in db/actions/agent_db.py.
# Both the class methods and the free-function API below consume these.
# ---------------------------------------------------------------------------

# Columns the caller is allowed to mutate via :func:`update_agent_db_field`
# and :meth:`AgentRepository.update_field`. Used both as an allowlist
# (anti-injection / anti-typo) and to centralise the JSON-serialisation
# rule for ``capabilities``.
#
# ``token`` is deliberately OFF this allowlist — it's the auth secret,
# and the surface for rotating it lives in :meth:`AgentRepository.rotate_token`
# (PR 8), not the generic field-update API.
# Server-side agent_id validation regex — matches the dashboard's
# client-side pattern verbatim. VM e2e on 2026-06-16 surfaced that
# `create_agent` accepted garbage IDs (`"InvalidName!@#"`) because the
# server enforced nothing while the dashboard form pinned this regex.
# Downstream consumers (URL routing, tmux session names, git worktree
# paths) assume slug shape; non-slug IDs are a poisoning vector.
#
# The two-branch alternation `|^[a-z]$` handles single-character names
# (the first branch requires at least two chars: a leading lowercase
# letter and a trailing lowercase letter/digit).
_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$|^[a-z]$")


_MUTABLE_FIELDS: set[str] = {
    "status",
    "current_task",
    "working_directory",
    "color",
    "capabilities",
    "updated_at",
    "aoe_session_id",
    # Event-coord PR-1 + PR-2.
    "auto_event_loop",
    "last_event_seen_at",
    # PR 8 (Agent flip): added so the restore-agent path
    # (``app.routes.restore_agent``) can clear ``terminated_at`` via
    # the repo's transaction-aware ``update_field`` seam instead of
    # owning a raw UPDATE.
    "terminated_at",
    # Phase 2 Wave 2b (plan §2e): the dashboard's Edit Agent modal
    # promotes a worker to manager (or demotes) by patching
    # ``agent_role``. The API-boundary check in
    # ``edit_agent_api_route`` already restricts the value to
    # {'worker', 'manager'} before reaching the repo; the column
    # CHECK constraint (Wave 1a) is the last-resort guard.
    "agent_role",
}


# ---------------------------------------------------------------------------
# Module-level free functions — formerly lived in db/actions/agent_db.py.
# These remain ORM-backed and behaviourally unchanged. They are exported
# unchanged via the ``db.actions.agent_db`` shim so legacy callers
# (``cli.py``, ``core.auth``, ``app.routes`` lifespan,
# ``core.repositories.agent_repo``, tests) keep working.
# ---------------------------------------------------------------------------


def _agent_to_dict(row: Agent) -> Dict[str, Any]:
    """Project an ``Agent`` ORM row into the dict shape consumers expect.

    Mirrors the pre-cutover ``dict(sqlite_row)`` projection: every column
    is exposed by name, and ``capabilities`` is JSON-decoded (the column
    stores a JSON-encoded list).
    """
    data: Dict[str, Any] = {
        "token": row.token,
        "agent_id": row.agent_id,
        "capabilities": row.capabilities,
        "created_at": row.created_at,
        "status": row.status,
        "current_task": row.current_task,
        "working_directory": row.working_directory,
        "color": row.color,
        "terminated_at": row.terminated_at,
        "updated_at": row.updated_at,
        "aoe_session_id": row.aoe_session_id,
        # Event-coord PR-1 columns. ``auto_event_loop`` defaults TRUE
        # for legacy rows via the migration's ``DEFAULT 1``;
        # ``last_event_seen_at`` is NULL until the agent first calls
        # ``fetch_events_since`` (PR-2).
        "auto_event_loop": getattr(row, "auto_event_loop", True),
        "last_event_seen_at": getattr(row, "last_event_seen_at", None),
        # Phase 2 Wave 2b: persisted by Wave 1a's migration with
        # ``DEFAULT 'worker'`` + CHECK in {'worker', 'manager'}.
        "agent_role": getattr(row, "agent_role", "worker"),
    }
    raw_caps = data.get("capabilities")
    if isinstance(raw_caps, str):
        try:
            data["capabilities"] = json.loads(raw_caps or "[]")
        except json.JSONDecodeError:
            logger.warning(
                f"Failed to parse capabilities JSON for agent "
                f"{row.agent_id!r}. Raw: {raw_caps!r}"
            )
            data["capabilities"] = []
    return data


def get_agent_by_id(agent_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single agent by agent_id. Returns None if not found."""
    try:
        with get_session() as session:
            row = (
                session.query(Agent)
                .filter(Agent.agent_id == agent_id)
                .one_or_none()
            )
            return _agent_to_dict(row) if row is not None else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching agent by ID '{agent_id}': {e}",
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error fetching agent by ID '{agent_id}': {e}",
            exc_info=True,
        )
        return None


def get_agent_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Fetch a single agent by bearer token. Returns None if not found."""
    try:
        with get_session() as session:
            row = (
                session.query(Agent)
                .filter(Agent.token == token)
                .one_or_none()
            )
            return _agent_to_dict(row) if row is not None else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching agent by token: {e}", exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error fetching agent by token: {e}", exc_info=True,
        )
        return None


def get_all_active_agents_from_db() -> List[Dict[str, Any]]:
    """Fetch every agent whose status is not 'terminated'.

    Used by ``application_startup`` to populate ``g.active_agents``.
    """
    try:
        with get_session() as session:
            rows = (
                session.query(Agent)
                .filter(Agent.status != "terminated")
                .all()
            )
            return [_agent_to_dict(r) for r in rows]
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching all active agents: {e}", exc_info=True,
        )
        return []
    except Exception as e:
        logger.error(
            f"Unexpected error fetching all active agents: {e}",
            exc_info=True,
        )
        return []


def update_agent_db_field(
    agent_id: str, field_name: str, new_value: Any,
) -> bool:
    """Update a single field on an agent row + bump ``updated_at``.

    Returns True on success, False on any failure (unknown field,
    no matching agent, or DB error). The allowlist is non-negotiable —
    callers must not be able to mutate ``token`` or ``agent_id`` /
    ``created_at`` via this surface.
    """
    if field_name not in _MUTABLE_FIELDS:
        logger.error(
            f"Attempted to update an invalid or unsupported agent "
            f"field: {field_name}"
        )
        return False

    value_to_set = new_value
    if field_name == "capabilities":
        # Event-coord PR-1: normalize at write time (strip + lowercase +
        # dedupe, preserve order of first occurrence). One source of
        # truth for both agents.capabilities and
        # tasks.required_capabilities (task_tools applies the same
        # helper). Read paths must NOT re-normalize.
        from ..utils.capability_normalization import normalize_capabilities

        value_to_set = json.dumps(normalize_capabilities(new_value))
    elif field_name == "auto_event_loop":
        # SQLite has no native bool; coerce any truthy value to 1, any
        # falsy to 0 so dashboard PATCH bodies (true/false/0/1) all
        # land as the integer the column expects.
        value_to_set = 1 if new_value else 0
    elif field_name == "updated_at" and new_value is None:
        value_to_set = datetime.datetime.now().isoformat()

    try:
        with get_session() as session:
            row = (
                session.query(Agent)
                .filter(Agent.agent_id == agent_id)
                .one_or_none()
            )
            if row is None:
                logger.warning(
                    f"Agent '{agent_id}' not found; "
                    f"field '{field_name}' update is a no-op."
                )
                return False
            setattr(row, field_name, value_to_set)
            # Always bump updated_at, even if the caller's field IS
            # updated_at (the value above already accounts for that).
            row.updated_at = datetime.datetime.now().isoformat()
            session.commit()
            logger.info(
                f"Agent '{agent_id}' field '{field_name}' updated in DB."
            )
            return True
    except SQLAlchemyError as e:
        logger.error(
            f"Database error updating agent '{agent_id}' field "
            f"'{field_name}': {e}",
            exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error updating agent '{agent_id}' field "
            f"'{field_name}': {e}",
            exc_info=True,
        )
        return False


# Convenience aliases used by the class below — kept private so the
# class-method code reads ``_db_get_agent_by_id(...)`` (matching the
# pre-flip naming) without re-importing from the shim.
_db_get_agent_by_id = get_agent_by_id
_db_get_agent_by_token = get_agent_by_token


class AgentRepository:
    """The class behind ``agent_mcp.repositories.agent_repo``.

    Instances are cheap and stateless — every method opens a fresh
    SQLAlchemy session via ``get_session()`` (the same pattern the
    existing ``agent_db`` helpers use). The class identity exists so
    callers can hold a reference, type-check against
    ``AgentRepository``, and (in future PRs) attach per-instance
    state like batching policies or audit hooks without rewriting
    every call site.

    The class is the **single owner** of the dual-cache+DB invariant
    for agents:

    1. Reads consult cache first (``state.active_agents`` keyed by
       token, ``state.agent_working_dirs`` keyed by agent_id), fall
       through to the DB on miss, warm both caches on the way back.
    2. Writes touch the DB first, then update both caches in lockstep,
       then publish to the EventBus. The order matters: a write that
       fails at the DB must not invalidate the caches, and a publish
       that fires before the cache update could race with a subscriber
       that immediately re-reads the cache.
    """

    # --- Test-mode flag --------------------------------------------------
    #
    # Mirrors the module-level flag on the legacy ``core.repositories.
    # agent_repo``. Tests that exercise DB-only behaviour can enter
    # ``disable_cache()`` to suspend cache interaction for the duration
    # of a ``with`` block.

    _cache_disabled: bool = False

    @contextlib.contextmanager
    def disable_cache(self) -> Iterator[None]:
        """Suspend cache reads/writes inside the ``with`` block.

        DB writes and EventBus publishes still happen — only the
        ``state.active_agents`` / ``state.agent_working_dirs``
        interaction is skipped. Useful for tests that want to verify
        DB-only behaviour without dealing with cache invariants.
        """
        prev = self._cache_disabled
        self._cache_disabled = True
        try:
            yield
        finally:
            self._cache_disabled = prev

    # --- Read interface --------------------------------------------------

    def _find_cached_by_agent_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Scan ``state.active_agents`` (keyed by token) for a row whose
        ``agent_id`` matches. The cache is small (one entry per active
        agent, typically < 100), so the O(n) scan is acceptable."""
        for data in state.active_agents.values():
            if data.get("agent_id") == agent_id:
                return data
        return None

    def get_by_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Fetch an agent by agent_id. Cache-first; falls through to DB.

        Warm-on-miss: a successful DB read populates
        ``state.active_agents`` (keyed by the token) and
        ``state.agent_working_dirs`` (keyed by agent_id) so subsequent
        ``get_by_token`` / ``get_working_directory`` calls are cache hits.
        """
        if not self._cache_disabled:
            cached = self._find_cached_by_agent_id(agent_id)
            if cached is not None:
                return cached

        row = _db_get_agent_by_id(agent_id)
        # SECURITY (terminate-revocation): never warm the auth cache with
        # a terminated row. The /mcp gate is cache-only and trusts that
        # active_agents holds only non-terminated rows; caching a
        # status='terminated' row would silently reactivate a revoked
        # bearer. Row still RETURNED for audit — only the write is gated.
        if (
            row is not None
            and not self._cache_disabled
            and row.get("status") != "terminated"
        ):
            token = row.get("token")
            if token:
                state.active_agents[token] = row
            wd = row.get("working_directory")
            if wd:
                state.agent_working_dirs[agent_id] = wd
        return row

    def get_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Fetch an agent by bearer token. Cache-first; falls through to DB.

        This is the auth hot path — ``verify_token()`` calls this on
        every incoming MCP request — so the cache hit is the common
        case.
        """
        if not self._cache_disabled:
            cached = state.active_agents.get(token)
            if cached is not None:
                return cached

        row = _db_get_agent_by_token(token)
        # SECURITY (terminate-revocation): see get_by_id above. A
        # terminated bearer resolved on the auth hot path must NOT be
        # re-inserted into the cache-only /mcp gate. Row still RETURNED
        # for audit; the cache write is gated on non-terminated status.
        if (
            row is not None
            and not self._cache_disabled
            and row.get("status") != "terminated"
        ):
            state.active_agents[token] = row
            agent_id = row.get("agent_id")
            wd = row.get("working_directory")
            if agent_id and wd:
                state.agent_working_dirs[agent_id] = wd
        return row

    def list_active(self) -> List[Dict[str, Any]]:
        """Return every non-terminated agent. DB-authoritative.

        ``state.active_agents`` is keyed by token and doesn't
        guarantee a full snapshot if startup-time hydration was
        skipped. Callers that need the full list (lifespan startup,
        dashboard ``/api/all-data``) go through here.
        """
        return get_all_active_agents_from_db()

    def query(
        self,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Run a filtered SELECT and return (page, total_count).

        Replaces the inline filter+count SQL in
        ``view_agents_tool_impl`` (admin_tools.py).

        Recognised filter keys:

        * ``status`` (str)            — exact status match
        * ``agent_id_pattern`` (str)  — LIKE pattern on ``agent_id``
        * ``include_terminated`` (bool, default True) — when False,
          excludes ``status='terminated'`` rows.
        * ``created_after`` (str)     — ``created_at >= created_after``
        * ``created_before`` (str)    — ``created_at <= created_before``
        * ``sort_by`` (str)           — one of {agent_id, status,
          created_at, terminated_at}; defaults to ``created_at``.
        * ``sort_order`` (str)        — ``ASC`` or ``DESC``;
          defaults to ``DESC``.
        * ``limit`` (int, default 50)
        * ``offset`` (int, default 0)

        Returns ``(rows, total)`` where ``rows`` is the page and
        ``total`` is the unfiltered row count for the filter set.
        On DB error returns ``([], 0)``.
        """
        from sqlalchemy import func

        filters = filters or {}
        allowed_sort = {
            "agent_id", "status", "created_at", "terminated_at",
        }
        sort_by = filters.get("sort_by", "created_at")
        if sort_by not in allowed_sort:
            sort_by = "created_at"
        sort_order = (filters.get("sort_order") or "DESC").upper()
        if sort_order not in ("ASC", "DESC"):
            sort_order = "DESC"
        limit = int(filters.get("limit", 50))
        offset = int(filters.get("offset", 0))
        if limit < 1:
            limit = 1
        if offset < 0:
            offset = 0
        include_terminated = filters.get("include_terminated", True)
        filter_status = filters.get("status")
        filter_pattern = filters.get("agent_id_pattern")
        filter_after = filters.get("created_after")
        filter_before = filters.get("created_before")

        try:
            with get_session() as session:
                q = session.query(Agent)
                if filter_status:
                    q = q.filter(Agent.status == filter_status)
                if filter_pattern:
                    q = q.filter(Agent.agent_id.like(filter_pattern))
                if not include_terminated:
                    q = q.filter(Agent.status != "terminated")
                if filter_after:
                    q = q.filter(Agent.created_at >= filter_after)
                if filter_before:
                    q = q.filter(Agent.created_at <= filter_before)

                total = q.with_entities(func.count(Agent.agent_id)).scalar() or 0

                sort_col = getattr(Agent, sort_by)
                if sort_order == "ASC":
                    q = q.order_by(sort_col.asc())
                else:
                    q = q.order_by(sort_col.desc())

                rows = q.limit(limit).offset(offset).all()
                return [
                    {
                        "token": r.token,
                        "agent_id": r.agent_id,
                        "status": r.status,
                        "created_at": r.created_at,
                    }
                    for r in rows
                ], int(total)
        except SQLAlchemyError as e:
            logger.error(
                f"Database error querying agents: {e}", exc_info=True,
            )
            return [], 0
        except Exception as e:
            logger.error(
                f"Unexpected error querying agents: {e}", exc_info=True,
            )
            return [], 0

    def get_working_directory(self, agent_id: str) -> Optional[str]:
        """Return the working directory for ``agent_id``, or ``None``.

        Looks up the ``agent_working_dirs`` cache first (the common
        path in file tools), then falls through to a full agent read.
        """
        if not self._cache_disabled:
            wd = state.agent_working_dirs.get(agent_id)
            if wd is not None:
                return wd
        row = self.get_by_id(agent_id)
        return row.get("working_directory") if row else None

    # --- Write interface: create ----------------------------------------

    def create(
        self,
        *,
        token: str,
        agent_id: str,
        capabilities: Optional[List[str]] = None,
        status: str = "created",
        current_task: Optional[str] = None,
        working_directory: str,
        color: Optional[str] = None,
        agent_role: str = "worker",
        connection: Any = None,
    ) -> Dict[str, Any]:
        """INSERT an agent row, update both caches, publish ``"agent.created"``.

        Required: ``token``, ``agent_id``, ``working_directory``.
        Optional: ``capabilities``, ``status``, ``current_task``,
        ``color``.

        ``connection`` is the transaction-aware seam. Tolerates a
        SQLAlchemy ``Session`` OR a raw ``sqlite3.Cursor`` so the
        caller's wider transaction stays atomic — used by
        ``create_agent_tool_impl`` which writes the agent row plus
        an ``agent_actions`` audit-log entry plus the task-assignment
        UPDATEs in one transaction. When ``None``, the method opens
        its own session.

        Returns the freshly-stored row in the same dict shape
        consumers expect (capabilities deserialised). On DB conflict
        (e.g. duplicate ``agent_id`` or duplicate ``token``), raises
        the underlying ``SQLAlchemyError``.

        Raises ``ValueError`` if ``agent_id`` doesn't match the slug
        regex (see ``_AGENT_ID_RE``) — caught at the seam so every
        caller (MCP tool, REST, CLI) hits the same check.
        """
        # VM e2e on 2026-06-16: `create_agent` accepted garbage IDs.
        # The repository is the single owner of this invariant — raise
        # BEFORE any DB write so no partial state is left behind.
        if not isinstance(agent_id, str) or not _AGENT_ID_RE.match(agent_id):
            raise ValueError(
                f"invalid agent_id {agent_id!r}: must match "
                f"{_AGENT_ID_RE.pattern} "
                f"(lowercase letters, digits, hyphens; must start with "
                f"a letter; must not end with a hyphen)."
            )

        now = datetime.datetime.now().isoformat()
        caps_json = json.dumps(capabilities or [])

        # Phase 2 Wave 2b: persist ``agent_role`` through every code
        # path (raw-cursor, caller-owned-session, repo-owned-session).
        # The column CHECK constraint (Wave 1a, v5.0.61) is the
        # last-resort guard; callers validate the value at the API
        # boundary before reaching the repo.
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                """
                INSERT INTO agents (
                    token, agent_id, capabilities, created_at, status,
                    current_task, working_directory, color,
                    terminated_at, updated_at, aoe_session_id, agent_role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token, agent_id, caps_json, now, status,
                    current_task, working_directory, color,
                    None, now, None, agent_role,
                ),
            )
        elif connection is not None:
            session = connection
            row = Agent(
                token=token,
                agent_id=agent_id,
                capabilities=caps_json,
                created_at=now,
                status=status,
                current_task=current_task,
                working_directory=working_directory,
                color=color,
                terminated_at=None,
                updated_at=now,
                aoe_session_id=None,
                agent_role=agent_role,
            )
            session.add(row)
            session.flush()
        else:
            with get_session() as session:
                row = Agent(
                    token=token,
                    agent_id=agent_id,
                    capabilities=caps_json,
                    created_at=now,
                    status=status,
                    current_task=current_task,
                    working_directory=working_directory,
                    color=color,
                    terminated_at=None,
                    updated_at=now,
                    aoe_session_id=None,
                    agent_role=agent_role,
                )
                session.add(row)
                session.commit()

        # Build the return dict. For the no-connection path we can
        # re-fetch via the legacy projection; for the connection paths
        # the row hasn't committed yet so build the dict in place.
        if connection is None:
            fresh = _db_get_agent_by_id(agent_id)
            if fresh is None:
                fresh = {
                    "token": token,
                    "agent_id": agent_id,
                    "capabilities": capabilities or [],
                    "created_at": now,
                    "status": status,
                    "current_task": current_task,
                    "working_directory": working_directory,
                    "color": color,
                    "terminated_at": None,
                    "updated_at": now,
                }
        else:
            fresh = {
                "token": token,
                "agent_id": agent_id,
                "capabilities": capabilities or [],
                "created_at": now,
                "status": status,
                "current_task": current_task,
                "working_directory": working_directory,
                "color": color,
                "terminated_at": None,
                "updated_at": now,
                "agent_role": agent_role,
            }

        # Cache + EventBus only on the standalone path. With a
        # ``connection=`` the caller's transaction is still open; a
        # publish or cache write before commit could be observed by
        # subscribers or persist after rollback. Caller is responsible
        # for calling :meth:`upsert_cache` after their own commit.
        if connection is None:
            if not self._cache_disabled:
                state.active_agents[token] = fresh
                state.agent_working_dirs[agent_id] = working_directory

            _publish(
                agent_id,
                "agent.created",
                {"agent_id": agent_id, "status": status},
            )
        return fresh

    # --- Write interface: update ----------------------------------------

    def update_field(
        self,
        agent_id: str,
        field_name: str,
        new_value: Any,
        *,
        connection: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """UPDATE one field via the allowlisted writer; refresh caches; publish.

        ``connection`` is the Risk #1 hook (mirrors
        ``TaskRepository.update_fields``): a handler that already
        holds an open SQLAlchemy ``Session`` in a wider transaction
        can pass it in to keep the write atomic with its surrounding
        statements. When ``None`` (the normal case), the call opens
        its own session via the existing ``update_agent_db_field``
        helper.

        Returns the post-update dict, or ``None`` if the row was
        unknown / field rejected by the allowlist / DB error. Matches
        the legacy module-of-functions semantics so callers that
        today branch on a falsy return don't need to change.

        Event type:
          * ``"agent.status_changed"`` when ``field_name == "status"``.
          * ``"agent.updated"`` for every other allowlisted field.
        """
        # sqlite3 cursor path: caller owns BEGIN/COMMIT. PR #152.
        if connection is not None and not hasattr(connection, "query"):
            ok = self._update_field_with_cursor(
                connection, agent_id, field_name, new_value,
            )
            if not ok:
                return None
            # Caller owns transaction — cache + publish deferred to
            # them (post-commit). Return a thin shape carrying only
            # the field that changed so they can wire it into their
            # own response.
            return {"agent_id": agent_id, field_name: new_value}

        if connection is not None:
            ok = self._update_field_with_session(
                connection, agent_id, field_name, new_value,
            )
        else:
            ok = update_agent_db_field(agent_id, field_name, new_value)
        if not ok:
            return None

        fresh = _db_get_agent_by_id(agent_id)
        if fresh is None:
            return None

        if not self._cache_disabled:
            token = fresh.get("token")
            if token:
                state.active_agents[token] = fresh
            wd = fresh.get("working_directory")
            if wd:
                state.agent_working_dirs[agent_id] = wd

        event_type = (
            "agent.status_changed"
            if field_name == "status"
            else "agent.updated"
        )
        _publish(
            agent_id,
            event_type,
            {"agent_id": agent_id, "field": field_name, "value": new_value},
        )
        return fresh

    def _update_field_with_cursor(
        self,
        cursor: Any,
        agent_id: str,
        field_name: str,
        new_value: Any,
    ) -> bool:
        """Internal helper for the sqlite3.Cursor ``connection=`` path.

        Mirrors the allowlist + capabilities-normalisation logic of
        :func:`update_agent_db_field` but writes via raw SQL against
        the caller's cursor so the wider BEGIN/COMMIT stays atomic.
        """
        if field_name not in _MUTABLE_FIELDS:
            logger.error(
                f"Attempted to update an invalid or unsupported agent "
                f"field via shared cursor: {field_name}"
            )
            return False

        value_to_set = new_value
        if field_name == "capabilities":
            from ..utils.capability_normalization import normalize_capabilities
            value_to_set = json.dumps(normalize_capabilities(new_value))
        elif field_name == "auto_event_loop":
            value_to_set = 1 if new_value else 0
        elif field_name == "updated_at" and new_value is None:
            value_to_set = datetime.datetime.now().isoformat()

        now = datetime.datetime.now().isoformat()
        try:
            cursor.execute(
                f"UPDATE agents SET {field_name} = ?, updated_at = ? "
                f"WHERE agent_id = ?",
                (value_to_set, now, agent_id),
            )
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(
                f"Database error updating agent '{agent_id}' field "
                f"'{field_name}' via shared cursor: {e}",
                exc_info=True,
            )
            return False

    def _update_field_with_session(
        self,
        session: Any,
        agent_id: str,
        field_name: str,
        new_value: Any,
    ) -> bool:
        """Internal helper for the ``connection=`` overload.

        Mirrors the allowlist + capabilities-normalisation logic
        ``update_agent_db_field`` does, but against the caller-provided
        session so the wider transaction stays intact. Kept private
        because the public API is
        ``update_field(agent_id, field, value, connection=)`` —
        exposing the session-shaped variant directly would leak a
        transient implementation detail.
        """
        if field_name not in _MUTABLE_FIELDS:
            logger.error(
                f"Attempted to update an invalid or unsupported agent "
                f"field via shared session: {field_name}"
            )
            return False

        value_to_set = new_value
        if field_name == "capabilities":
            from ..utils.capability_normalization import normalize_capabilities

            value_to_set = json.dumps(normalize_capabilities(new_value))
        elif field_name == "auto_event_loop":
            value_to_set = 1 if new_value else 0
        elif field_name == "updated_at" and new_value is None:
            value_to_set = datetime.datetime.now().isoformat()

        try:
            row = (
                session.query(Agent)
                .filter(Agent.agent_id == agent_id)
                .one_or_none()
            )
            if row is None:
                return False
            setattr(row, field_name, value_to_set)
            row.updated_at = datetime.datetime.now().isoformat()
            session.flush()
            return True
        except SQLAlchemyError as e:
            logger.error(
                f"Database error updating agent '{agent_id}' field "
                f"'{field_name}' via shared session: {e}",
                exc_info=True,
            )
            return False

    # --- Write interface: terminate -------------------------------------

    def terminate(
        self,
        agent_id: str,
        *,
        connection: Any = None,
    ) -> bool:
        """Set status='terminated', evict both caches, publish.

        Returns True on success, False if the row didn't exist, was
        already terminated, or the DB raised. Cache eviction happens
        after the DB commit so a failed commit doesn't desync the
        in-memory state.

        ``connection`` is the transaction-aware seam. Tolerates a
        SQLAlchemy ``Session`` OR a raw ``sqlite3.Cursor`` so
        ``terminate_agent_tool_impl`` (which writes the status
        update + an ``agent_actions`` audit row in one transaction)
        can keep them atomic. When ``None``, the method opens its
        own session.
        """
        terminated_at = datetime.datetime.now().isoformat()
        token: Optional[str] = None

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                "SELECT token, status FROM agents WHERE agent_id = ?",
                (agent_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            try:
                row_token = row["token"]
                row_status = row["status"]
            except (KeyError, IndexError):
                row_token, row_status = row[0], row[1]
            if row_status == "terminated":
                return False
            token = row_token
            cur.execute(
                """
                UPDATE agents
                SET status = ?, terminated_at = ?, updated_at = ?,
                    current_task = NULL
                WHERE agent_id = ? AND status != ?
                """,
                ("terminated", terminated_at, terminated_at,
                 agent_id, "terminated"),
            )
            if cur.rowcount == 0:
                return False
        elif connection is not None:
            session = connection
            try:
                row = (
                    session.query(Agent)
                    .filter(Agent.agent_id == agent_id)
                    .filter(Agent.status != "terminated")
                    .one_or_none()
                )
                if row is None:
                    return False
                token = row.token
                row.status = "terminated"
                row.terminated_at = terminated_at
                row.updated_at = terminated_at
                row.current_task = None
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error terminating agent '{agent_id}' via "
                    f"shared session: {e}",
                    exc_info=True,
                )
                return False
        else:
            try:
                with get_session() as session:
                    row = (
                        session.query(Agent)
                        .filter(Agent.agent_id == agent_id)
                        .filter(Agent.status != "terminated")
                        .one_or_none()
                    )
                    if row is None:
                        return False
                    token = row.token
                    row.status = "terminated"
                    row.terminated_at = terminated_at
                    row.updated_at = terminated_at
                    row.current_task = None
                    session.commit()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error terminating agent '{agent_id}': {e}",
                    exc_info=True,
                )
                return False

        # Cache + EventBus only on the standalone path. With a
        # ``connection=`` the caller still owns the transaction; the
        # cache eviction and publish are deferred to the caller's
        # post-commit step (typically :meth:`evict_from_cache` for
        # cache, then their own publish or a follow-up call).
        if connection is None:
            if not self._cache_disabled:
                state.agent_working_dirs.pop(agent_id, None)
                if token is not None:
                    state.active_agents.pop(token, None)
                else:
                    # token unknown: scan to find the right entry.
                    for cached_token, data in list(
                        state.active_agents.items()
                    ):
                        if data.get("agent_id") == agent_id:
                            state.active_agents.pop(cached_token, None)

            _publish(
                agent_id,
                "agent.terminated",
                {"agent_id": agent_id, "terminated_at": terminated_at},
            )
        return True

    # --- Write interface: bulk filter UPDATE ----------------------------

    def clear_current_task_for(
        self,
        task_id: str,
        *,
        connection: Any = None,
    ) -> int:
        """Clear ``current_task`` on every agent pointing at ``task_id``.

        Replaces the filter-based ``UPDATE agents SET current_task =
        NULL WHERE current_task = ?`` raw SQL from ``task_tools._update_
        single_task`` and the bulk-update path. Used when a task reaches
        a terminal state (completed/cancelled/failed) — without this
        sweep, ``agents.current_task`` keeps pointing at a stale row
        and leaks into ``/api/all-data`` and the dashboard's
        "current task" indicator.

        Filter-based by design: the predicate is on ``current_task``,
        not on ``agent_id``. ``update_field`` is keyed by ``agent_id``
        and is the wrong surface for this — the caller doesn't know
        which agents need clearing without an extra SELECT.

        Returns the number of rows updated (0 when nothing pointed at
        the task). On DB error returns 0.

        ``connection`` is the transaction-aware seam (sqlite3 ``Cursor``
        or SQLAlchemy ``Session``) so the call stays atomic with the
        wider task-status UPDATE that triggers it. Cache mirror happens
        on every path: in-memory ``state.active_agents`` entries whose
        ``current_task == task_id`` get cleared so the next tool call
        sees the update without waiting for a lifespan reload.
        """
        updated_at = datetime.datetime.now().isoformat()
        rowcount = 0

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            try:
                cur.execute(
                    "UPDATE agents SET current_task = NULL, updated_at = ? "
                    "WHERE current_task = ?",
                    (updated_at, task_id),
                )
                rowcount = cur.rowcount or 0
            except Exception as e:
                logger.error(
                    f"Database error clearing current_task for task "
                    f"'{task_id}' via shared cursor: {e}",
                    exc_info=True,
                )
                return 0
        elif connection is not None:
            session = connection
            try:
                q = session.query(Agent).filter(Agent.current_task == task_id)
                rowcount = q.update(
                    {Agent.current_task: None, Agent.updated_at: updated_at},
                    synchronize_session=False,
                )
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error clearing current_task for task "
                    f"'{task_id}' via shared session: {e}",
                    exc_info=True,
                )
                return 0
        else:
            try:
                with get_session() as session:
                    q = session.query(Agent).filter(
                        Agent.current_task == task_id,
                    )
                    rowcount = q.update(
                        {
                            Agent.current_task: None,
                            Agent.updated_at: updated_at,
                        },
                        synchronize_session=False,
                    )
                    session.commit()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error clearing current_task for task "
                    f"'{task_id}': {e}",
                    exc_info=True,
                )
                return 0

        # Cache mirror. Safe to do on every path because the cache write
        # only sets a field on entries that already point at ``task_id``;
        # if the surrounding transaction rolls back the next read will
        # re-warm from the DB (which still has the stale value, but
        # current_task points at a terminal task — semantically still
        # consistent with what the caller observed).
        if not self._cache_disabled:
            for entry in state.active_agents.values():
                if entry.get("current_task") == task_id:
                    entry["current_task"] = None
        return int(rowcount)

    # --- Write interface: rotate_token ----------------------------------

    def rotate_token(
        self,
        agent_id: str,
        new_token: str,
        *,
        connection: Any = None,
    ) -> bool:
        """Replace an agent's bearer token. Re-keys ``state.active_agents``.

        ``token`` is deliberately OFF the ``update_field`` allowlist
        (the generic field-update API shouldn't be able to overwrite
        the auth secret). This dedicated method exists for the
        admin-relaunch flow that legitimately needs to rotate.

        Re-keys ``state.active_agents``: the cache is keyed by token,
        so a token change requires popping the old key and inserting
        the new one. ``state.agent_working_dirs`` (keyed by agent_id)
        is unaffected.

        ``connection`` is the transaction-aware seam — the
        admin-relaunch flow wraps token rotation, status flip, and an
        audit-log INSERT in one transaction. When ``None``, the call
        opens its own session.

        Returns True on success, False if the agent didn't exist or
        the DB raised.
        """
        updated_at = datetime.datetime.now().isoformat()
        old_token: Optional[str] = None

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            try:
                cur.execute(
                    "SELECT token FROM agents WHERE agent_id = ?",
                    (agent_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                try:
                    old_token = row["token"]
                except (KeyError, IndexError):
                    old_token = row[0]
                cur.execute(
                    "UPDATE agents SET token = ?, updated_at = ? "
                    "WHERE agent_id = ?",
                    (new_token, updated_at, agent_id),
                )
                if cur.rowcount == 0:
                    return False
            except Exception as e:
                logger.error(
                    f"Database error rotating token for agent '{agent_id}' "
                    f"via shared cursor: {e}",
                    exc_info=True,
                )
                return False
        elif connection is not None:
            session = connection
            try:
                row = (
                    session.query(Agent)
                    .filter(Agent.agent_id == agent_id)
                    .one_or_none()
                )
                if row is None:
                    return False
                old_token = row.token
                row.token = new_token
                row.updated_at = updated_at
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error rotating token for agent '{agent_id}' "
                    f"via shared session: {e}",
                    exc_info=True,
                )
                return False
        else:
            try:
                with get_session() as session:
                    row = (
                        session.query(Agent)
                        .filter(Agent.agent_id == agent_id)
                        .one_or_none()
                    )
                    if row is None:
                        return False
                    old_token = row.token
                    row.token = new_token
                    row.updated_at = updated_at
                    session.commit()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error rotating token for agent "
                    f"'{agent_id}': {e}",
                    exc_info=True,
                )
                return False

        # Re-key the cache only on the standalone path. With a
        # ``connection=`` the caller's transaction is still open; the
        # cache re-key + publish are deferred to the caller's
        # post-commit step. (For the relaunch flow, the caller's own
        # cache write after commit covers this; this method's job is
        # the DB write + the public method's contract.)
        if connection is None:
            if not self._cache_disabled and old_token is not None:
                cached = state.active_agents.pop(old_token, None)
                if cached is not None:
                    cached["token"] = new_token
                    state.active_agents[new_token] = cached
            _publish(
                agent_id,
                "agent.updated",
                {"agent_id": agent_id, "field": "token", "value": new_token},
            )
        return True

    # --- Write interface: delete ----------------------------------------

    def delete(
        self,
        agent_id: str,
        *,
        connection: Any = None,
    ) -> bool:
        """Hard-delete an agent row + evict both caches.

        Unlike :meth:`terminate` (which flips status='terminated' and
        leaves the row for audit), ``delete`` removes the row entirely.
        Used by:

        * The testing-agent cleanup in ``task_tools.complete_task_tool_impl``
          (a re-completed task replaces the previous testing agent — the
          old row is hard-deleted because the agent_id is recycled).
        * The purge-agent admin flow in ``app.routes.purge_agent`` (FK
          cascades + tombstone rewrites have run; the agents row
          disappears LAST).

        ``connection`` is the transaction-aware seam — both call sites
        wrap the DELETE in a wider transaction (testing-agent: also
        INSERTs the new agent + task-assignment UPDATEs; purge: also
        rewrites FKs across agent_messages / tasks / agent_actions).

        Returns True if a row was deleted, False if no agent matched
        the agent_id (and on DB error).
        """
        token: Optional[str] = None

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            try:
                cur.execute(
                    "SELECT token FROM agents WHERE agent_id = ?",
                    (agent_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                try:
                    token = row["token"]
                except (KeyError, IndexError):
                    token = row[0]
                cur.execute(
                    "DELETE FROM agents WHERE agent_id = ?", (agent_id,),
                )
                if cur.rowcount == 0:
                    return False
            except Exception as e:
                logger.error(
                    f"Database error deleting agent '{agent_id}' via "
                    f"shared cursor: {e}",
                    exc_info=True,
                )
                return False
        elif connection is not None:
            session = connection
            try:
                row = (
                    session.query(Agent)
                    .filter(Agent.agent_id == agent_id)
                    .one_or_none()
                )
                if row is None:
                    return False
                token = row.token
                session.delete(row)
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error deleting agent '{agent_id}' via "
                    f"shared session: {e}",
                    exc_info=True,
                )
                return False
        else:
            try:
                with get_session() as session:
                    row = (
                        session.query(Agent)
                        .filter(Agent.agent_id == agent_id)
                        .one_or_none()
                    )
                    if row is None:
                        return False
                    token = row.token
                    session.delete(row)
                    session.commit()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error deleting agent '{agent_id}': {e}",
                    exc_info=True,
                )
                return False

        # Cache eviction. Safe on every path — if the surrounding
        # transaction rolls back the next read will re-warm from the
        # DB (the row will still exist; cache miss is the cost).
        if not self._cache_disabled:
            state.agent_working_dirs.pop(agent_id, None)
            if token is not None:
                state.active_agents.pop(token, None)
            else:
                for cached_token, data in list(state.active_agents.items()):
                    if data.get("agent_id") == agent_id:
                        state.active_agents.pop(cached_token, None)

        if connection is None:
            _publish(
                agent_id,
                "agent.deleted",
                {"agent_id": agent_id},
            )
        return True

    # --- Write interface: tombstone INSERT ------------------------------

    def insert_tombstone(
        self,
        *,
        token: str,
        tombstone_agent_id: str,
        connection: Any = None,
    ) -> None:
        """INSERT OR IGNORE a synthetic tombstone agents row.

        Used by the purge-agent flow to satisfy the FK from
        ``agent_messages.{sender_id, recipient_id}`` after the original
        agent row is deleted: messages whose sender/recipient was the
        purged agent get rewritten to point at this tombstone string
        instead, which must exist as an agents row for the FK to hold.

        ``INSERT OR IGNORE`` so a re-purge (same agent_id, already
        tombstoned) is a no-op.

        Tombstone rows live alongside real agents but are off the
        active-agents cache by construction: they have ``status =
        'tombstone'`` so ``list_active()`` (which excludes
        ``status != 'terminated'`` — but tombstones are neither
        active nor terminated; they're tombstones) won't surface them.
        Existing read paths that look up by token will resolve the
        ``__tombstone_<id>`` namespaced token to this row, but no real
        bearer can present that token because it's reserved.

        ``connection`` is the transaction-aware seam — the purge flow
        wraps tombstone INSERT, FK rewrites across agent_messages /
        tasks / agent_actions, and the original-row DELETE in one
        transaction.
        """
        now = datetime.datetime.now().isoformat()
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                "INSERT OR IGNORE INTO agents "
                "(token, agent_id, capabilities, created_at, status, "
                " working_directory, color, updated_at) "
                "VALUES (?, ?, '[]', ?, 'tombstone', '', '#000000', ?)",
                (token, tombstone_agent_id, now, now),
            )
            return
        if connection is not None:
            # Session path: SQLAlchemy ORM has no INSERT-OR-IGNORE,
            # so emulate via a pre-existence check.
            session = connection
            existing = (
                session.query(Agent)
                .filter(Agent.agent_id == tombstone_agent_id)
                .one_or_none()
            )
            if existing is not None:
                return
            session.add(
                Agent(
                    token=token,
                    agent_id=tombstone_agent_id,
                    capabilities="[]",
                    created_at=now,
                    status="tombstone",
                    working_directory="",
                    color="#000000",
                    terminated_at=None,
                    updated_at=now,
                    aoe_session_id=None,
                )
            )
            session.flush()
            return
        # Standalone path: open our own session.
        with get_session() as session:
            existing = (
                session.query(Agent)
                .filter(Agent.agent_id == tombstone_agent_id)
                .one_or_none()
            )
            if existing is not None:
                return
            session.add(
                Agent(
                    token=token,
                    agent_id=tombstone_agent_id,
                    capabilities="[]",
                    created_at=now,
                    status="tombstone",
                    working_directory="",
                    color="#000000",
                    terminated_at=None,
                    updated_at=now,
                    aoe_session_id=None,
                )
            )
            session.commit()

    # --- Cache-only helpers ---------------------------------------------
    #
    # These exist so the legacy raw-SQL call sites in ``admin_tools.py`` can
    # keep their wider transactions intact while still routing cache
    # mutation through the repository. Once the multi-table transactional
    # surfaces migrate to ``create`` / ``update_field`` / ``terminate``
    # proper, these can be removed.

    def upsert_cache(self, row: Dict[str, Any]) -> None:
        """Insert/overwrite a single cache entry without going through the DB.

        Used by lifespan startup to bulk-load the cache from the DB,
        and by legacy writers that own their own raw SQL INSERT inside
        a wider transaction.
        """
        if self._cache_disabled:
            return
        token = row.get("token")
        agent_id = row.get("agent_id")
        if not token or not agent_id:
            logger.warning(
                "agent_repo.upsert_cache called without token or agent_id; "
                "ignoring"
            )
            return
        state.active_agents[token] = row
        wd = row.get("working_directory")
        if wd:
            state.agent_working_dirs[agent_id] = wd

    def evict_from_cache(
        self, agent_id: str, *, token: Optional[str] = None,
    ) -> None:
        """Evict both caches for an agent. SQL DELETE/UPDATE owned by caller.

        Used by legacy terminate / purge paths that wrap the agent row
        mutation in a multi-table transaction with ``agent_actions`` /
        ``agent_messages``.
        """
        if self._cache_disabled:
            return
        state.agent_working_dirs.pop(agent_id, None)
        if token is not None:
            state.active_agents.pop(token, None)
            return
        # token unknown: scan to find the right entry.
        for cached_token, data in list(state.active_agents.items()):
            if data.get("agent_id") == agent_id:
                state.active_agents.pop(cached_token, None)


__all__ = ["AgentRepository"]
