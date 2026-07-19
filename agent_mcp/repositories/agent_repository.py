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
* ``agent_mcp.db.actions.agent_db`` was a re-export shim that kept
  legacy importers (``cli.py``, ``core.auth``, ``app.routes`` lifespan,
  the older module-of-functions repo under ``core.repositories``,
  ``tests/test_sqlalchemy_agent.py``) working unchanged. arch-deepening
  R3 #2b deleted the shim (it re-exported nothing of its own) and
  repointed every importer at this module directly.
* ``AgentRepository`` is the single owner — handler → repo → SQL.
"""
from __future__ import annotations

import contextlib
import datetime
import re
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError

from ..core import state
from ..core.config import logger
# NOTE: we import the bus shim lazily inside the publish call sites
# below. Historically a top-level
# ``from ..core.repositories import _event_bus_shim`` would execute
# ``core.repositories.__init__``, which eagerly imported the legacy
# module-of-functions ``core.repositories.agent_repo``, which in turn
# imported ``db.actions.agent_db`` — a shim that re-exported from THIS
# module — producing a circular import at first load. arch-deepening
# R3 #2a deleted ``agent_repo`` and #2b deleted ``db.actions.agent_db``
# outright (closing that cycle for good) and relocated the bus shim to
# ``core.event_bus_shim``. Kept lazy anyway: a function-local import
# costs nothing at this call frequency.
from ..db.engine import get_session
from ..db.models import Agent

# ---------------------------------------------------------------------------
# Canonical "is this a live agent?" predicate (arch-deepening F).
#
# WHY this exists: the same concept was expressed two ways across the
# codebase and the two variants DRIFTED. The strict form
# ``status NOT IN ('terminated', 'tombstone')`` correctly excludes both
# soft-deleted agents AND ``[deleted-<id>]`` tombstone rows (purge-cascade
# FK artefacts, reserved ``__tombstone_*`` tokens — see ``insert_tombstone``).
# A weaker ``status != 'terminated'`` variant lingered on a few surfaces and
# LET TOMBSTONE ROWS LEAK THROUGH (e.g. a tombstone bearer surfacing on the
# operator token listing). One fragment + one helper is the single source of
# truth so the weak variant cannot re-appear.
#
# ``LIVE_AGENT_SQL`` is a WHERE-clause fragment built ONLY from trusted
# literals (no interpolation of caller input) — safe to f-string into raw
# SQL. ``TERMINAL_AGENT_STATUSES`` is the Python-side companion for
# in-memory status checks. ``is_live_agent`` is the raw-cursor point lookup.
TERMINAL_AGENT_STATUSES: Tuple[str, ...] = ("terminated", "tombstone")
LIVE_AGENT_SQL = "status NOT IN ('terminated', 'tombstone')"


def is_live_status(status: Optional[str]) -> bool:
    """True iff ``status`` denotes a live agent (not terminated/tombstone)."""
    return status not in TERMINAL_AGENT_STATUSES


def is_live_agent(agent_id: str, cursor: Any) -> bool:
    """True iff a live (non-terminated, non-tombstone) agent row exists.

    ``cursor`` is a raw sqlite cursor (the caller owns the connection).
    Uses :data:`LIVE_AGENT_SQL` so this predicate can never drift from
    the other converged sites.
    """
    cursor.execute(
        f"SELECT 1 FROM agents WHERE agent_id = ? AND {LIVE_AGENT_SQL}",
        (agent_id,),
    )
    return cursor.fetchone() is not None


def _publish(addressee: str, event: str, payload: Dict[str, Any]) -> None:
    """Lazy-import shim around ``event_bus_shim.publish``.

    See the module-level NOTE above the imports for why this stays a
    function-local import (a stale cycle this used to dodge, closed by
    arch-deepening R3 #2a/#2b).
    """
    from ..core import event_bus_shim

    event_bus_shim.publish(addressee, event, payload)


# ---------------------------------------------------------------------------
# Module-level constants — formerly lived in db/actions/agent_db.py.
# Both the class methods and the free-function API below consume these.
# ---------------------------------------------------------------------------

# Columns the caller is allowed to mutate via :func:`update_agent_db_field`
# and :meth:`AgentRepository.update_field`. Used as an allowlist
# (anti-injection / anti-typo) and to centralise per-field normalization.
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

# Reserved agent_id prefixes. Several authorization gates privilege an
# agent purely by the agent_id STRING rather than by role — e.g.
# ``agent_id == "admin"`` (messaging / read-any-inbox / authorize) and
# ``agent_id.lower().startswith("admin")`` (task_tools / state). A
# worker-role row literally named "admin" (or "admin-x") would inherit
# those name-keyed privileges, so the repository — the single owner of
# the agent_id invariant — refuses to mint one. Case-insensitive
# prefix match to mirror the ``.lower().startswith("admin")`` gates.
_RESERVED_AGENT_ID_PREFIXES: tuple[str, ...] = ("admin",)


def _is_reserved_agent_id(agent_id: str) -> bool:
    lowered = agent_id.lower()
    return any(lowered.startswith(p) for p in _RESERVED_AGENT_ID_PREFIXES)


_MUTABLE_FIELDS: set[str] = {
    "status",
    "current_task",
    "working_directory",
    "color",
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


def _sanitise_field(field_name: str, new_value: Any) -> Tuple[bool, Any]:
    """Allowlist-check ``field_name`` and normalize ``new_value``.

    The single source of truth for the ``_MUTABLE_FIELDS`` allowlist
    plus the per-field normalization rules (``auto_event_loop`` -> 1/0,
    a ``None`` ``updated_at`` -> "now"). Before arch-r5 #3 this block was
    duplicated 3x (the standalone own-session writer, the shared-cursor
    writer, and a dead shared-session writer that no caller ever
    exercised — see the module docstring / PR notes). Both surviving
    writers call this so the invariant can't drift between them again.

    Returns ``(True, normalized_value)`` when ``field_name`` is
    allowed, or ``(False, None)`` when it must be rejected.
    """
    if field_name not in _MUTABLE_FIELDS:
        return False, None

    value_to_set = new_value
    if field_name == "auto_event_loop":
        # SQLite has no native bool; coerce any truthy value to 1, any
        # falsy to 0 so dashboard PATCH bodies (true/false/0/1) all
        # land as the integer the column expects.
        value_to_set = 1 if new_value else 0
    elif field_name == "updated_at" and new_value is None:
        value_to_set = datetime.datetime.now().isoformat()

    return True, value_to_set


# ---------------------------------------------------------------------------
# Module-level free functions — formerly lived in db/actions/agent_db.py.
# These remain ORM-backed and behaviourally unchanged. Legacy callers
# (``cli.py``, ``core.auth``, ``app.routes`` lifespan, tests) used to
# reach them via the ``db.actions.agent_db`` re-export shim;
# arch-deepening R3 #2b deleted that shim and repointed every importer
# at this module directly.
# ---------------------------------------------------------------------------


def _agent_to_dict(row: Agent) -> Dict[str, Any]:
    """Project an ``Agent`` ORM row into the dict shape consumers expect.

    Mirrors the pre-cutover ``dict(sqlite_row)`` projection: every column
    is exposed by name.
    """
    data: Dict[str, Any] = {
        "token": row.token,
        "agent_id": row.agent_id,
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
        # Agent self-service profiles (migration 0018). All nullable;
        # NULL ``profile`` = never set, NULL ``profile_reviewed_at`` =
        # overdue for review.
        "profile": getattr(row, "profile", None),
        "profile_updated_at": getattr(row, "profile_updated_at", None),
        "profile_reviewed_at": getattr(row, "profile_reviewed_at", None),
        "profile_updated_by": getattr(row, "profile_updated_by", None),
    }
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
    """Fetch every active agent.

    "Active" excludes both ``'terminated'`` (soft-deleted) and
    ``'tombstone'`` (purge-cascade FK artefacts). Tombstone rows exist
    only to keep ``agent_messages`` FKs valid after a purge (see
    ``insert_tombstone``); they are neither agents nor terminated
    agents and must never surface in a listing — mirrors the REST
    ``WHERE status != 'tombstone'`` filter so MCP and REST share one
    active-agents contract (BL-R31-3).

    Used by ``application_startup`` to populate ``g.active_agents``.
    """
    try:
        with get_session() as session:
            rows = (
                session.query(Agent)
                .filter(Agent.status.notin_(TERMINAL_AGENT_STATUSES))
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


def count_active_agents_by_status_from_db() -> Dict[str, int]:
    """Return ``{status: count}`` over the non-terminal (active-set)
    agents via a SQL ``GROUP BY`` — never materialises the rows.

    pentest R4-F1: ``GET /api/status`` used to count agents by
    materialising every non-terminal agent
    (:func:`get_all_active_agents_from_db`, a bare ``.all()``) and
    running ``len()`` / filter comprehensions on the result. This mirrors
    that function's ``WHERE status NOT IN ('terminated','tombstone')``
    filter (``TERMINAL_AGENT_STATUSES``) but counts in SQL, so the
    caller derives ``total_agents`` (sum of the values) and
    ``active_agents`` (``.get('active', 0)``) without reading a single
    agent row into Python. On DB error returns ``{}`` and logs at error.
    """
    from sqlalchemy import func

    try:
        with get_session() as session:
            rows = (
                session.query(Agent.status, func.count(Agent.token))
                .filter(Agent.status.notin_(TERMINAL_AGENT_STATUSES))
                .group_by(Agent.status)
                .all()
            )
            return {status: int(count) for status, count in rows}
    except SQLAlchemyError as e:
        logger.error(
            f"Database error counting active agents by status: {e}",
            exc_info=True,
        )
        return {}
    except Exception as e:
        logger.error(
            f"Unexpected error counting active agents by status: {e}",
            exc_info=True,
        )
        return {}


def update_agent_db_field(
    agent_id: str, field_name: str, new_value: Any,
) -> bool:
    """Update a single field on an agent row + bump ``updated_at``.

    Returns True on success, False on any failure (unknown field,
    no matching agent, or DB error). The allowlist is non-negotiable —
    callers must not be able to mutate ``token`` or ``agent_id`` /
    ``created_at`` via this surface.
    """
    ok, value_to_set = _sanitise_field(field_name, new_value)
    if not ok:
        logger.error(
            f"Attempted to update an invalid or unsupported agent "
            f"field: {field_name}"
        )
        return False

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
        # a non-active row. The /mcp gate is cache-only and trusts that
        # active_agents holds only active rows; caching a
        # status='terminated' row would silently reactivate a revoked
        # bearer, and a status='tombstone' row (BL-R31-3b) would leak the
        # purge FK artefact into the operator token listing. Row still
        # RETURNED for audit — only the write is gated.
        if (
            row is not None
            and not self._cache_disabled
            and row.get("status") not in TERMINAL_AGENT_STATUSES
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
        # non-active bearer resolved on the auth hot path must NOT be
        # re-inserted into the cache-only /mcp gate. Row still RETURNED
        # for audit; the cache write is gated on active status
        # (excludes 'terminated' AND 'tombstone' — BL-R31-3b).
        if (
            row is not None
            and not self._cache_disabled
            and row.get("status") not in TERMINAL_AGENT_STATUSES
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

        Reserved for reconciliation / warm / boot paths (arch-r5 #7).
        Anything asking "is agent X active right now" for an
        auth-adjacent decision must go through :meth:`active_agent_ids`
        instead — this method's fresh DB read can disagree with the
        ``state.active_agents`` cache the ``/mcp`` auth gate trusts
        (e.g. mid-flight between a terminate's DB commit and its
        cache eviction), and that DB/cache pair is exactly the
        two-sources-of-truth split #7 closes for every OTHER caller.
        """
        return get_all_active_agents_from_db()

    def count_active_by_status(self) -> Dict[str, int]:
        """Return ``{status: count}`` over the non-terminal (active-set)
        agents via a SQL aggregate.

        DB-authoritative. Used by ``GET /api/status`` so a dashboard
        poll counts agents without materialising the agents table
        (pentest R4-F1 — see
        :func:`count_active_agents_by_status_from_db`). The caller reads
        ``total_agents`` as the sum of the values and ``active_agents``
        as ``.get('active', 0)`` — identical semantics to the previous
        ``len(list_active())`` / ``len([a for a in ... if
        a['status']=='active'])`` counts.
        """
        return count_active_agents_by_status_from_db()

    def active_agent_ids(self) -> set[str]:
        """Set of ``agent_id`` for every agent in the LIVE auth cache.

        The single owner of "which agents are active" (arch-r5 #7).
        Projects ``state.active_agents`` — the SAME cache
        ``app.main_app._bearer_is_active`` gates ``/mcp`` auth against
        and ``admin_tools.view_status`` / the broadcast fan-out iterate
        directly — from its token-keyed shape down to the ``agent_id``
        set callers actually want. Because every one of those reads the
        identical in-memory dict, they cannot disagree with each other:
        there is one cache, one owner method for the id-set projection
        of it, and every "is X active" / "which agents are active"
        question resolves through the same object.

        Deliberately NOT a DB query — see :meth:`list_active` for the
        DB-authoritative counterpart reserved for reconciliation/warm/
        boot, where a fresh snapshot (rather than auth-cache agreement)
        is the point.
        """
        return {
            data["agent_id"]
            for data in state.active_agents.values()
            if data.get("agent_id")
        }

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
                # BL-R31-3: tombstone rows (purge-cascade FK artefacts,
                # created by insert_tombstone) are NEVER listable agents.
                # Exclude them unconditionally — before the count — so
                # MCP view_agents matches the REST agent-list surfaces
                # (routers/agents.py applies `WHERE status != 'tombstone'`
                # regardless of any status filter, and refuses
                # `status=tombstone`). Combined with an explicit
                # `status='tombstone'` filter this yields the empty set,
                # mirroring `GET /api/agents?status=tombstone` → [].
                q = q.filter(Agent.status != "tombstone")
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
        status: str = "created",
        current_task: Optional[str] = None,
        working_directory: str,
        color: Optional[str] = None,
        agent_role: str = "worker",
        connection: Any = None,
    ) -> Dict[str, Any]:
        """INSERT an agent row, update both caches, publish ``"agent.created"``.

        Required: ``token``, ``agent_id``, ``working_directory``.
        Optional: ``status``, ``current_task``, ``color``.

        ``connection`` is the transaction-aware seam. Tolerates a
        SQLAlchemy ``Session`` OR a raw ``sqlite3.Cursor`` so the
        caller's wider transaction stays atomic — used by
        ``create_agent_tool_impl`` which writes the agent row plus
        an ``agent_actions`` audit-log entry plus the task-assignment
        UPDATEs in one transaction. When ``None``, the method opens
        its own session.

        Returns the freshly-stored row in the same dict shape
        consumers expect. On DB conflict (e.g. duplicate ``agent_id`` or
        duplicate ``token``), raises the underlying ``SQLAlchemyError``.

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

        # Reserved-name guard (Wave-B): reject names that name-keyed
        # authorization gates would privilege. See
        # ``_RESERVED_AGENT_ID_PREFIXES``. Raised BEFORE any DB write so
        # no partial state leaks, matching the slug-regex guard above.
        if _is_reserved_agent_id(agent_id):
            raise ValueError(
                f"reserved agent_id {agent_id!r}: names beginning with "
                f"{_RESERVED_AGENT_ID_PREFIXES!r} are reserved for "
                f"privileged / built-in identities and cannot be "
                f"assigned to an agent."
            )

        now = datetime.datetime.now().isoformat()

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
                    token, agent_id, created_at, status,
                    current_task, working_directory, color,
                    terminated_at, updated_at, aoe_session_id, agent_role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token, agent_id, now, status,
                    current_task, working_directory, color,
                    None, now, None, agent_role,
                ),
            )
        elif connection is not None:
            session = connection
            row = Agent(
                token=token,
                agent_id=agent_id,
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
            # SECURITY (terminate-revocation): see get_by_id / get_by_token
            # above. ``status`` defaults to 'created' and today's only
            # caller (``register_agent``) always passes that, but the
            # write path itself must not rely on caller discipline — a
            # future caller handing ``create()`` a terminal status must
            # not land the row in the cache-only auth gate (pentest
            # R1-F4 class-sweep; ``upsert_cache`` had the identical gap).
            if (
                not self._cache_disabled
                and status not in TERMINAL_AGENT_STATUSES
            ):
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
        connection: Optional[sqlite3.Cursor] = None,
    ) -> Optional[Dict[str, Any]]:
        """UPDATE one field via the allowlisted writer; refresh caches; publish.

        ``connection`` is the Risk #1 hook: a handler that already
        holds an open raw ``sqlite3.Cursor`` in a wider transaction
        (its own BEGIN/COMMIT) can pass it in to keep the write atomic
        with its surrounding statements. When ``None`` (the normal
        case), the call opens its own session via the existing
        ``update_agent_db_field`` helper.

        arch-r5 #3: a SQLAlchemy ``Session`` overload used to be
        accepted here too (disambiguated via ``hasattr(connection,
        "query")``), but a grep of every ``update_field(...,
        connection=...)`` call site across ``agent_mcp/`` and
        ``tests/`` turned up zero Session-shaped callers — every one
        passes a raw cursor. Removed the dead branch and typed the
        parameter honestly.

        Returns the post-update dict, or ``None`` if the row was
        unknown / field rejected by the allowlist / DB error. Matches
        the legacy module-of-functions semantics so callers that
        today branch on a falsy return don't need to change.

        Event type:
          * ``"agent.status_changed"`` when ``field_name == "status"``.
          * ``"agent.updated"`` for every other allowlisted field.
        """
        # sqlite3 cursor path: caller owns BEGIN/COMMIT. PR #152.
        if connection is not None:
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

        ok = update_agent_db_field(agent_id, field_name, new_value)
        if not ok:
            return None

        fresh = _db_get_agent_by_id(agent_id)
        if fresh is None:
            return None

        # SECURITY (terminate-revocation): see get_by_id / get_by_token
        # above. A row read back after an update must not be re-warmed
        # into the cache-only auth gate once it's terminal — e.g. a
        # concurrent terminate flipped status underneath this write.
        # Row still RETURNED for the caller; only the cache write is
        # gated.
        if (
            not self._cache_disabled
            and fresh.get("status") not in TERMINAL_AGENT_STATUSES
        ):
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

    def advance_event_cursor(self, agent_id: str, cursor_value: str) -> bool:
        """Monotonically advance ``last_event_seen_at`` — never regress.

        Unlike :meth:`update_field` (last-writer-wins), this issues
        ``SET last_event_seen_at = MAX(COALESCE(last_event_seen_at, ''), ?)``
        so a stale / lower cursor from a slow concurrent ``wait_for_events``
        waiter can't rewind the high-water mark and replay events that
        were already delivered. Timestamps are ISO-8601 strings, so the
        lexicographic ``MAX`` matches the chronological ordering the
        event stream compares on elsewhere.

        ``COALESCE(..., '')`` guards the first write while the column is
        still NULL: SQLite's scalar ``max()`` returns NULL if *any*
        argument is NULL, which would otherwise swallow the first cursor
        and leave the column NULL forever. ``''`` sorts before any real
        ISO timestamp, so the incoming value always wins on the first
        write.

        Returns True when the agent row exists (rowcount > 0), False on
        unknown agent, empty ``cursor_value``, or DB error. Refreshes the
        caches and publishes ``agent.updated`` ONLY when the watermark
        actually moved — a no-op advance (equal/lower cursor) publishes
        nothing, so it can't self-wake every sibling ``wait_for_events``
        waiter under fan-out (arch-r2 #2a).
        """
        if not cursor_value:
            return False

        from sqlalchemy import func, select as sa_select, update as sa_update

        now = datetime.datetime.now().isoformat()
        try:
            with get_session() as session:
                # Read the prior watermark in the same session so we can
                # tell a real advance from a no-op (MAX keeps the higher
                # value, so a stale/equal cursor changes nothing).
                prev_value = session.execute(
                    sa_select(Agent.last_event_seen_at).where(
                        Agent.agent_id == agent_id
                    )
                ).scalar_one_or_none()
                result = session.execute(
                    sa_update(Agent)
                    .where(Agent.agent_id == agent_id)
                    .values(
                        last_event_seen_at=func.max(
                            func.coalesce(Agent.last_event_seen_at, ""),
                            cursor_value,
                        ),
                        updated_at=now,
                    )
                )
                session.commit()
                if (result.rowcount or 0) == 0:
                    return False
        except SQLAlchemyError as e:
            logger.error(
                f"Database error advancing event cursor for agent "
                f"'{agent_id}': {e}",
                exc_info=True,
            )
            return False

        fresh = _db_get_agent_by_id(agent_id)
        if fresh is None:
            return False

        # SECURITY (terminate-revocation): see get_by_id / get_by_token
        # above. An in-flight ``wait_for_events`` waiter can resume
        # after its agent was terminated and land here (via
        # ``_write_last_event_seen_at``) — the row it reads back is now
        # terminal and must NOT be re-warmed into the cache-only auth
        # gate, or the termination's revocation is silently undone.
        # Row still RETURNED (rowcount semantics unchanged); only the
        # cache write is gated.
        if (
            not self._cache_disabled
            and fresh.get("status") not in TERMINAL_AGENT_STATUSES
        ):
            token = fresh.get("token")
            if token:
                state.active_agents[token] = fresh
            wd = fresh.get("working_directory")
            if wd:
                state.agent_working_dirs[agent_id] = wd

        # SELF-WAKE FIX (arch-r2 #2a): only publish ``agent.updated`` when
        # the watermark ACTUALLY moved. ``last_event_seen_at`` is a
        # poll-internal high-water mark nobody consumes as an agent event;
        # publishing it wakes every sibling ``wait_for_events`` waiter
        # (agent.updated → state.notify_waiters) to re-query for nothing.
        # Under fan-out, N concurrent waiters each re-write the same cursor
        # → O(N) spurious wakes per event round. Emitting only on a real
        # value change collapses the no-op re-writes to zero wakes while
        # preserving the notify on a genuine advance.
        new_value = fresh.get("last_event_seen_at")
        if new_value != prev_value:
            _publish(
                agent_id,
                "agent.updated",
                {
                    "agent_id": agent_id,
                    "field": "last_event_seen_at",
                    "value": new_value,
                },
            )
        return True

    def review_profile(
        self,
        agent_id: str,
        *,
        new_profile: Optional[str] = None,
        editor_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Record a profile review; bump content bookkeeping iff it changed.

        Two timestamps, one write (agent self-service profiles, plan §2):

        * ``profile_reviewed_at`` is ALWAYS set to now — even a no-op
          confirm counts as a review, and this is what drives the
          staleness nudge. A forced weekly review that changes nothing
          moves ONLY this, so peers are never spammed by a no-op.
        * ``profile`` / ``profile_updated_at`` / ``profile_updated_by``
          move ONLY when ``new_profile`` is provided AND its sha256
          differs from the stored profile. ``profile_updated_at`` is what
          the peer-broadcast keys on, so a real content change is the
          only thing that reaches other agents.

        ``new_profile=None`` (arg omitted by the caller) is the "confirm
        still accurate" path: ``reviewed_at`` moves, nothing else.

        Returns the post-review dict with an extra ``"changed": bool`` key,
        or ``None`` when the agent row is unknown / a DB error occurred.
        On a real content change refreshes the caches (so a cached auth
        row carries the fresh profile) and publishes ``agent.updated``.
        """
        import hashlib

        def _hash(value: Optional[str]) -> str:
            return hashlib.sha256((value or "").encode("utf-8")).hexdigest()

        now = datetime.datetime.now().isoformat()
        changed = False
        try:
            with get_session() as session:
                row = (
                    session.query(Agent)
                    .filter(Agent.agent_id == agent_id)
                    .one_or_none()
                )
                if row is None:
                    return None
                # Always record the review.
                row.profile_reviewed_at = now
                # Content change gate: only a real diff bumps updated_at
                # + updated_by (and thus reaches the peer broadcast).
                if (
                    new_profile is not None
                    and _hash(new_profile) != _hash(row.profile)
                ):
                    row.profile = new_profile
                    row.profile_updated_at = now
                    row.profile_updated_by = editor_id
                    changed = True
                # Keep the generic updated_at moving too (mirrors every
                # other write path in this repo).
                row.updated_at = now
                session.commit()
        except SQLAlchemyError as e:
            logger.error(
                f"Database error reviewing profile for agent "
                f"'{agent_id}': {e}",
                exc_info=True,
            )
            return None

        fresh = _db_get_agent_by_id(agent_id)
        if fresh is None:
            return None
        fresh["changed"] = changed

        # Refresh the caches so a cached auth row carries the fresh
        # profile. Gated on non-terminal status exactly like every other
        # write path (terminate-revocation invariant).
        if (
            not self._cache_disabled
            and fresh.get("status") not in TERMINAL_AGENT_STATUSES
        ):
            token = fresh.get("token")
            if token:
                state.active_agents[token] = fresh
            wd = fresh.get("working_directory")
            if wd:
                state.agent_working_dirs[agent_id] = wd

        # Only a real content change is worth waking sibling waiters /
        # publishing — a no-op review moves reviewed_at only.
        if changed:
            _publish(
                agent_id,
                "agent.updated",
                {"agent_id": agent_id, "field": "profile"},
            )
        return fresh

    def _update_field_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        agent_id: str,
        field_name: str,
        new_value: Any,
    ) -> bool:
        """Internal helper for the sqlite3.Cursor ``connection=`` path.

        Uses :func:`_sanitise_field` — the same allowlist +
        normalisation :func:`update_agent_db_field` uses — but writes
        via raw SQL against the caller's cursor so the wider
        BEGIN/COMMIT stays atomic.
        """
        ok, value_to_set = _sanitise_field(field_name, new_value)
        if not ok:
            logger.error(
                f"Attempted to update an invalid or unsupported agent "
                f"field via shared cursor: {field_name}"
            )
            return False

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
            # BL-R31-3b: a 'tombstone' row (`[deleted-<id>]` purge FK
            # artefact) is not a live agent and must not be
            # terminatable — flipping it to 'terminated' would leak the
            # artefact into the terminated-agents listing. Treat it (and
            # an already-terminated row) as not-found.
            if row_status in TERMINAL_AGENT_STATUSES:
                return False
            token = row_token
            cur.execute(
                f"""
                UPDATE agents
                SET status = ?, terminated_at = ?, updated_at = ?,
                    current_task = NULL
                WHERE agent_id = ? AND {LIVE_AGENT_SQL}
                """,
                ("terminated", terminated_at, terminated_at, agent_id),
            )
            if cur.rowcount == 0:
                return False
        elif connection is not None:
            session = connection
            try:
                row = (
                    session.query(Agent)
                    .filter(Agent.agent_id == agent_id)
                    # BL-R31-3b: exclude 'tombstone' (purge FK artefact)
                    # too — it is not a live, terminatable agent.
                    .filter(Agent.status.notin_(TERMINAL_AGENT_STATUSES))
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
                        # BL-R31-3b: exclude 'tombstone' too — not a
                        # live, terminatable agent.
                        .filter(Agent.status.notin_(TERMINAL_AGENT_STATUSES))
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

    def clear_current_task_for_many(
        self,
        task_ids: List[str],
        *,
        connection: Any = None,
    ) -> int:
        """Clear ``current_task`` on every agent pointing at any of ``task_ids``.

        The set-valued sibling of :meth:`clear_current_task_for`. Used by
        ``task_tools.delete_task_tool_impl``'s ``force_delete`` path: a task
        (the delete target OR a cascaded descendant) referenced by some
        agent's ``current_task`` would otherwise abort the
        ``DELETE FROM tasks`` on the ``agents.current_task → tasks.task_id``
        FK, so ``force_delete`` must NULL those pointers in the SAME
        transaction, before the DELETE, for the force to actually force.

        Single ``UPDATE ... WHERE current_task IN (...)`` so N descendants
        cost one statement, not N. Empty ``task_ids`` is a no-op (returns
        0). Cache mirror matches :meth:`clear_current_task_for`: entries
        whose ``current_task`` is in the set get cleared in place.

        ``connection`` is the transaction-aware seam (sqlite3 ``Cursor``
        or SQLAlchemy ``Session``) so the call stays atomic with the wider
        delete transaction. Returns the number of rows updated. On DB
        error returns 0.
        """
        if not task_ids:
            return 0
        # Dedupe while preserving determinism; the IN-list only needs the
        # distinct set.
        id_set = list(dict.fromkeys(task_ids))
        updated_at = datetime.datetime.now().isoformat()
        rowcount = 0

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            placeholders = ",".join("?" for _ in id_set)
            try:
                cur.execute(
                    f"UPDATE agents SET current_task = NULL, updated_at = ? "
                    f"WHERE current_task IN ({placeholders})",
                    (updated_at, *id_set),
                )
                rowcount = cur.rowcount or 0
            except Exception as e:
                logger.error(
                    f"Database error clearing current_task for tasks "
                    f"{id_set!r} via shared cursor: {e}",
                    exc_info=True,
                )
                return 0
        elif connection is not None:
            session = connection
            try:
                q = session.query(Agent).filter(
                    Agent.current_task.in_(id_set),
                )
                rowcount = q.update(
                    {Agent.current_task: None, Agent.updated_at: updated_at},
                    synchronize_session=False,
                )
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error clearing current_task for tasks "
                    f"{id_set!r} via shared session: {e}",
                    exc_info=True,
                )
                return 0
        else:
            try:
                with get_session() as session:
                    q = session.query(Agent).filter(
                        Agent.current_task.in_(id_set),
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
                    f"Database error clearing current_task for tasks "
                    f"{id_set!r}: {e}",
                    exc_info=True,
                )
                return 0

        if not self._cache_disabled:
            id_lookup = set(id_set)
            for entry in state.active_agents.values():
                if entry.get("current_task") in id_lookup:
                    entry["current_task"] = None
        return int(rowcount)

    def reconcile_current_task_on_reassign(
        self,
        task_id: str,
        prior_assignee: Optional[str],
        new_assignee: Optional[str],
        *,
        connection: Any = None,
    ) -> None:
        """Reconcile ``agents.current_task`` when a task is reassigned.

        The terminal-status path clears ``current_task`` via
        :meth:`clear_current_task_for`, but a REBIND (task moved from
        agent X to agent Y with no status change) reconciled neither
        pointer, so:

          * the LOSING agent kept a stale ``current_task`` pointing at a
            task it no longer owns (BL-R30-1 — the exact leak the
            terminal-clear guard was added for), and
          * the GAINING agent's ``current_task`` was never set, so it
            rendered idle in ``/api/all-data`` and the dashboard despite
            owning the task.

        This mirrors the two halves of the existing behaviour:

          * clear side — mirrors :meth:`clear_current_task_for`, but
            scoped to the LOSING agent only (``agent_id = prior AND
            current_task = task_id``) so a rebind never disturbs an
            unrelated agent whose ``current_task`` happens to differ.
          * set side — mirrors the canonical assign path
            (``task_tools._assign_to_existing_tasks``): set the gaining
            agent's ``current_task`` to ``task_id`` ONLY when it is
            currently ``NULL`` (never clobber a different in-flight
            pointer the agent already holds).

        Safe in the degenerate cases: ``prior == new`` is a no-op (the
        clear would immediately be re-set to the same value; both sides
        are guarded to avoid a spurious write); a clear-assignment
        (``new_assignee is None``) clears only the loser; a fresh assign
        (``prior_assignee is None``) sets only the gainer.

        ``connection`` is the transaction-aware seam (sqlite3 ``Cursor``
        or SQLAlchemy ``Session``) so the reconcile stays atomic with the
        ``tasks.assigned_to`` write that triggers it. Cache mirror on
        ``state.active_agents`` matches the sibling helpers. Best-effort
        on the write: a DB error is logged, not raised, so it can never
        poison the surrounding reassign transaction.
        """
        # prior == new (including None == None) → nothing moved.
        if prior_assignee == new_assignee:
            return

        updated_at = datetime.datetime.now().isoformat()

        clear_target = prior_assignee
        set_target = new_assignee

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            try:
                if clear_target is not None:
                    cur.execute(
                        "UPDATE agents SET current_task = NULL, "
                        "updated_at = ? "
                        "WHERE agent_id = ? AND current_task = ?",
                        (updated_at, clear_target, task_id),
                    )
                if set_target is not None:
                    cur.execute(
                        "UPDATE agents SET current_task = ?, "
                        "updated_at = ? "
                        "WHERE agent_id = ? AND current_task IS NULL",
                        (task_id, updated_at, set_target),
                    )
            except Exception as e:
                logger.error(
                    f"Database error reconciling current_task on reassign "
                    f"of task '{task_id}' ({prior_assignee!r} -> "
                    f"{new_assignee!r}) via shared cursor: {e}",
                    exc_info=True,
                )
                return
        elif connection is not None:
            session = connection
            try:
                if clear_target is not None:
                    session.query(Agent).filter(
                        Agent.agent_id == clear_target,
                        Agent.current_task == task_id,
                    ).update(
                        {Agent.current_task: None, Agent.updated_at: updated_at},
                        synchronize_session=False,
                    )
                if set_target is not None:
                    session.query(Agent).filter(
                        Agent.agent_id == set_target,
                        Agent.current_task.is_(None),
                    ).update(
                        {Agent.current_task: task_id, Agent.updated_at: updated_at},
                        synchronize_session=False,
                    )
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error reconciling current_task on reassign "
                    f"of task '{task_id}' ({prior_assignee!r} -> "
                    f"{new_assignee!r}) via shared session: {e}",
                    exc_info=True,
                )
                return
        else:
            try:
                with get_session() as session:
                    if clear_target is not None:
                        session.query(Agent).filter(
                            Agent.agent_id == clear_target,
                            Agent.current_task == task_id,
                        ).update(
                            {
                                Agent.current_task: None,
                                Agent.updated_at: updated_at,
                            },
                            synchronize_session=False,
                        )
                    if set_target is not None:
                        session.query(Agent).filter(
                            Agent.agent_id == set_target,
                            Agent.current_task.is_(None),
                        ).update(
                            {
                                Agent.current_task: task_id,
                                Agent.updated_at: updated_at,
                            },
                            synchronize_session=False,
                        )
                    session.commit()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error reconciling current_task on reassign "
                    f"of task '{task_id}' ({prior_assignee!r} -> "
                    f"{new_assignee!r}): {e}",
                    exc_info=True,
                )
                return

        # Cache mirror: match the sibling helpers so the next tool call
        # sees the update without waiting for a lifespan reload.
        if not self._cache_disabled:
            for entry in state.active_agents.values():
                aid = entry.get("agent_id")
                if (
                    clear_target is not None
                    and aid == clear_target
                    and entry.get("current_task") == task_id
                ):
                    entry["current_task"] = None
                if (
                    set_target is not None
                    and aid == set_target
                    and entry.get("current_task") is None
                ):
                    entry["current_task"] = task_id

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
        # cache re-key + publish — and the stream teardown below — are
        # deferred to the caller's post-commit step (they must also
        # tear down the old bearer's live streams themselves).
        if connection is None:
            if not self._cache_disabled and old_token is not None:
                cached = state.active_agents.pop(old_token, None)
                # SECURITY (terminate-revocation): see get_by_id /
                # get_by_token above. Don't carry a terminal row
                # forward under the new token — that would re-warm the
                # cache-only auth gate for a bearer that should stay
                # revoked.
                if (
                    cached is not None
                    and cached.get("status") not in TERMINAL_AGENT_STATUSES
                ):
                    cached["token"] = new_token
                    state.active_agents[new_token] = cached

            # AC-R29-1: the old bearer's already-open GET /mcp streams
            # authenticated once at open and pump indefinitely; without
            # an active nudge they'd survive the rotation until their
            # next heartbeat self-validation tick. Signal them to
            # re-validate now, same as ``terminate``.
            try:
                from ..core import session_registry

                session_registry.close_streams_for_agent(agent_id)
            except Exception:  # pragma: no cover - defensive
                logger.warning(
                    "Failed to signal stream close for agent '%s' "
                    "after token rotation.",
                    agent_id,
                    exc_info=True,
                )

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
        'tombstone'``, which the active-agents listing filter
        (``get_all_active_agents_from_db`` / ``query``) excludes
        alongside ``'terminated'`` (BL-R31-3), so neither
        ``list_active()`` nor the MCP ``view_agents`` query surfaces
        them. Existing read paths that look up by token will resolve the
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
                "(token, agent_id, created_at, status, "
                " working_directory, color, updated_at) "
                "VALUES (?, ?, ?, 'tombstone', '', '#000000', ?)",
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
        # SECURITY (terminate-revocation): see get_by_id / get_by_token
        # above. A caller-supplied row headed straight for the cache-only
        # auth gate must not carry a terminal status — caching a
        # status='terminated' (or 'tombstone' — BL-R31-3b) row would
        # silently reactivate a revoked bearer. Today's only caller
        # (``register_agent``'s post-commit warm) always passes
        # status='created', but the gate belongs on the write path, not
        # on caller discipline (pentest R1-F4).
        if row.get("status") in TERMINAL_AGENT_STATUSES:
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
