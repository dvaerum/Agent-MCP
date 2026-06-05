# Agent-MCP/agent_mcp/db/actions/agent_db.py
"""Reusable DB operations for the `agents` table.

Cutover to SQLAlchemy in db-review PR-G2 — the model lives in
`agent_mcp.db.models.agent::Agent`. The function signatures + return
shapes (Optional[Dict[str, Any]] / List[Dict[str, Any]] keyed by
column name) are preserved 1:1 so consumers (tool authorisation,
lifespan startup, dashboard API) don't need to change.

The raw-SQL update path in `update_agent_db_field` keeps its
allowlist of mutable fields — that allowlist doubles as anti-injection
protection. The ORM cutover replaces the `f"UPDATE ... SET {field} = ?"`
template with `setattr(row, field, value)` on the Agent instance, but
the allowlist guard stays.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError

from ...core.config import logger
from ..engine import get_session
from ..models import Agent


# Columns the caller is allowed to mutate via `update_agent_db_field`.
# Used both as an allowlist (anti-injection / anti-typo) and to
# centralise the JSON-serialisation rule for `capabilities`.
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
}


def _agent_to_dict(row: Agent) -> Dict[str, Any]:
    """Project an `Agent` ORM row into the dict shape consumers expect.

    Mirrors the pre-cutover `dict(sqlite_row)` projection: every column
    is exposed by name, and `capabilities` is JSON-decoded (the column
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
        # Event-coord PR-1 columns. `auto_event_loop` defaults TRUE for
        # legacy rows via the migration's `DEFAULT 1`; `last_event_seen_at`
        # is NULL until the agent first calls `fetch_events_since` (PR-2).
        "auto_event_loop": getattr(row, "auto_event_loop", True),
        "last_event_seen_at": getattr(row, "last_event_seen_at", None),
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

    Used by `application_startup` to populate `g.active_agents`.
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
    """Update a single field on an agent row + bump `updated_at`.

    Returns True on success, False on any failure (unknown field,
    no matching agent, or DB error). The allowlist is non-negotiable —
    callers must not be able to mutate `token`, `agent_id`, or
    `created_at` via this surface.
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
        from ...utils.capability_normalization import normalize_capabilities

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
