# Agent-MCP/agent_mcp/core/repositories/agent_repo.py
"""AgentRepository — owns the ``state.active_agents`` cache plus
``state.agent_working_dirs``, alongside DB I/O for the ``agents``
table.

Why two caches in one repo:

``state.active_agents`` is keyed by **token** (the bearer secret used
for auth on every MCP call). ``state.agent_working_dirs`` is keyed by
**agent_id** (the human-readable name). They are two views over the
same row in the DB; keeping them in one repo means a single write
keeps both in sync (the legacy bug fixed in PR #130 was caused by
these two caches getting out of step with the DB).

Public contract:

Reads
    * :func:`get_agent_by_id` — cache-first lookup by agent_id.
    * :func:`get_agent_by_token` — cache-first lookup by token (auth
      hot path).
    * :func:`list_active_agents` — DB-authoritative listing of every
      non-terminated agent.

Writes
    * :func:`create_agent` — DB insert via ORM, then update both
      caches, then publish ``"agent.created"``.
    * :func:`update_agent_field` — delegate to the existing
      :func:`agent_db.update_agent_db_field`, then refresh cache,
      publish ``"agent.updated"`` (specialised to
      ``"agent.status_changed"`` when ``field_name == "status"``).
    * :func:`remove_agent_from_cache` — evict-only helper for the
      terminate / purge paths that already own the SQL DELETE in a
      multi-table transaction.

Test mode
    * :func:`disable_cache` — context manager. Skips both caches.
"""
from __future__ import annotations

import contextlib
import datetime
import json
from typing import Any, Dict, Iterator, List, Optional

from .. import state
from ..config import logger
from ...db.actions.agent_db import (
    get_agent_by_id as _db_get_agent_by_id,
    get_agent_by_token as _db_get_agent_by_token,
    get_all_active_agents_from_db,
    update_agent_db_field,
)
from ...db.engine import get_session
from ...db.models import Agent
from . import _event_bus_shim


_cache_disabled: bool = False


@contextlib.contextmanager
def disable_cache() -> Iterator[None]:
    """Suspend cache reads/writes for the duration of the ``with`` block.

    Inside the block:

    * Reads skip ``state.active_agents`` / ``state.agent_working_dirs``
      and go straight to DB.
    * Writes do NOT touch either cache.
    * The DB write and the bus publish still happen.
    """
    global _cache_disabled
    prev = _cache_disabled
    _cache_disabled = True
    try:
        yield
    finally:
        _cache_disabled = prev


def reset() -> None:
    """Clear both caches. For test isolation. Does NOT touch the DB."""
    state.active_agents.clear()
    state.agent_working_dirs.clear()


# --- read interface -----------------------------------------------------


def _find_cached_by_agent_id(agent_id: str) -> Optional[Dict[str, Any]]:
    """Scan ``state.active_agents`` (keyed by token) for a row whose
    ``agent_id`` matches. The cache is small (one entry per active
    agent, typically < 100), so the O(n) scan is acceptable."""
    for data in state.active_agents.values():
        if data.get("agent_id") == agent_id:
            return data
    return None


def get_agent_by_id(agent_id: str) -> Optional[Dict[str, Any]]:
    """Fetch an agent by agent_id. Cache-first; DB on miss."""
    if not _cache_disabled:
        cached = _find_cached_by_agent_id(agent_id)
        if cached is not None:
            return cached

    row = _db_get_agent_by_id(agent_id)
    if row is not None and not _cache_disabled:
        token = row.get("token")
        if token:
            state.active_agents[token] = row
        wd = row.get("working_directory")
        if wd:
            state.agent_working_dirs[agent_id] = wd
    return row


def get_agent_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Fetch an agent by bearer token. Cache-first; DB on miss.

    This is the auth hot path — verify_token() calls this on every
    incoming MCP request — so the cache hit is the common case.
    """
    if not _cache_disabled:
        cached = state.active_agents.get(token)
        if cached is not None:
            return cached

    row = _db_get_agent_by_token(token)
    if row is not None and not _cache_disabled:
        state.active_agents[token] = row
        agent_id = row.get("agent_id")
        wd = row.get("working_directory")
        if agent_id and wd:
            state.agent_working_dirs[agent_id] = wd
    return row


def list_active_agents() -> List[Dict[str, Any]]:
    """Return every non-terminated agent. DB-authoritative.

    Used by lifespan startup to populate the cache, and by the
    dashboard's /api/all-data route.
    """
    return get_all_active_agents_from_db()


def get_working_directory(agent_id: str) -> Optional[str]:
    """Return the working directory for ``agent_id``, or ``None`` if
    the agent is unknown.

    Looks up the agent_working_dirs cache first (the most common
    path in file tools), then falls through to a full agent read.
    """
    if not _cache_disabled:
        wd = state.agent_working_dirs.get(agent_id)
        if wd is not None:
            return wd
    row = get_agent_by_id(agent_id)
    return row.get("working_directory") if row else None


# --- write interface ----------------------------------------------------


def create_agent(
    *,
    token: str,
    agent_id: str,
    capabilities: Optional[List[str]] = None,
    status: str = "created",
    current_task: Optional[str] = None,
    working_directory: str,
    color: Optional[str] = None,
) -> Dict[str, Any]:
    """INSERT an agent row, update both caches, publish ``"agent.created"``."""
    now = datetime.datetime.now().isoformat()
    caps_json = json.dumps(capabilities or [])

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

    fresh = _db_get_agent_by_id(agent_id)
    if fresh is None:
        # Defensive fallback; should not happen.
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

    if not _cache_disabled:
        state.active_agents[token] = fresh
        state.agent_working_dirs[agent_id] = working_directory

    _event_bus_shim.publish(
        agent_id,
        "agent.created",
        {"agent_id": agent_id, "status": status},
    )
    return fresh


def update_agent_field(
    agent_id: str, field_name: str, new_value: Any,
) -> Optional[Dict[str, Any]]:
    """UPDATE one field via the ORM-allowlist writer, refresh caches,
    publish ``"agent.status_changed"`` (if field is ``status``) or
    ``"agent.updated"`` otherwise.
    """
    ok = update_agent_db_field(agent_id, field_name, new_value)
    if not ok:
        return None

    fresh = _db_get_agent_by_id(agent_id)
    if fresh is None:
        return None

    if not _cache_disabled:
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


def upsert_cache(row: Dict[str, Any]) -> None:
    """Insert/overwrite a single cache entry without going through the DB.

    Used by lifespan startup to bulk-load the cache from the DB, and
    by legacy writers that own their own raw SQL INSERT.
    """
    if _cache_disabled:
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


def remove_agent_from_cache(
    agent_id: str, *, token: Optional[str] = None,
) -> None:
    """Evict both caches for an agent. SQL DELETE is owned by the caller.

    Used by terminate / purge paths that wrap the agents row delete
    in a multi-table transaction with agent_actions / agent_messages.
    """
    if _cache_disabled:
        return
    state.agent_working_dirs.pop(agent_id, None)
    if token is not None:
        state.active_agents.pop(token, None)
        return
    # token unknown: scan to find the right entry.
    for cached_token, data in list(state.active_agents.items()):
        if data.get("agent_id") == agent_id:
            state.active_agents.pop(cached_token, None)


__all__ = [
    "create_agent",
    "disable_cache",
    "get_agent_by_id",
    "get_agent_by_token",
    "get_working_directory",
    "list_active_agents",
    "remove_agent_from_cache",
    "reset",
    "update_agent_field",
    "upsert_cache",
]
