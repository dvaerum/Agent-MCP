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

The class delegates DB I/O to the existing helpers in
:mod:`agent_mcp.db.actions.agent_db` and the SQLAlchemy ORM layer
behind them — no SQL gets re-written here. The class is the *seam*
between business logic and persistence, not a re-implementation of
either.
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
from ..db.actions.agent_db import (
    _MUTABLE_FIELDS,
    get_agent_by_id as _db_get_agent_by_id,
    get_agent_by_token as _db_get_agent_by_token,
    get_all_active_agents_from_db,
    update_agent_db_field,
)
from ..db.engine import get_session
from ..db.models import Agent


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
        if row is not None and not self._cache_disabled:
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
        if row is not None and not self._cache_disabled:
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
        """
        now = datetime.datetime.now().isoformat()
        caps_json = json.dumps(capabilities or [])

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                """
                INSERT INTO agents (
                    token, agent_id, capabilities, created_at, status,
                    current_task, working_directory, color,
                    terminated_at, updated_at, aoe_session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token, agent_id, caps_json, now, status,
                    current_task, working_directory, color,
                    None, now, None,
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

            _event_bus_shim.publish(
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
        _event_bus_shim.publish(
            agent_id,
            event_type,
            {"agent_id": agent_id, "field": field_name, "value": new_value},
        )
        return fresh

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

            _event_bus_shim.publish(
                agent_id,
                "agent.terminated",
                {"agent_id": agent_id, "terminated_at": terminated_at},
            )
        return True

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
