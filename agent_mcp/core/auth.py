# Agent-MCP/mcp_template/mcp_server_src/core/auth.py
import secrets
from typing import Any, Dict, Optional

# Original location: main.py, lines 852-854
def generate_token() -> str:
    """Generate a secure random token."""
    return secrets.token_hex(16)


# Original location: main.py, lines 868-873
def get_agent_id(token: str) -> Optional[str]:
    """
    Get agent ID from token.

    Resolves identity for audit-log attribution and per-tool ownership
    checks — NOT an auth gate.

    Migrated to ``agent_repo.get_agent_by_token`` in PR-W2c so a token
    for a row that's only in the DB (e.g. just restored by a peer
    process) resolves correctly without waiting for the next lifespan
    reload. The repo keeps the cache-hit semantics for the common
    case via ``state.active_agents``.
    """
    if not token: # Added a check for empty/None token
        return None
    # Local import to keep the legacy module-load contract: callers
    # that only want get_agent_id shouldn't pay the cost of loading
    # the SQLAlchemy engine until the first DB-miss path.
    from .repositories import agent_repo

    agent_data = agent_repo.get_agent_by_token(token)
    if isinstance(agent_data, dict) and "agent_id" in agent_data:
        return agent_data["agent_id"]
    return None


def query_agent_status(token: str) -> Optional[Dict[str, Any]]:
    """If `token` matches a row in the `agents` table, return its
    identifying status info; else return None.

    Purpose
    -------
    The in-memory ``g.active_agents`` map is rebuilt on startup from
    live rows only (the canonical ``LIVE_AGENT_SQL`` predicate —
    excludes both ``'terminated'`` and ``'tombstone'``). A bearer for a
    terminated agent therefore fails the per-request auth check and is
    indistinguishable from a freshly-invented unknown token — both
    produce the same generic 401.

    ``AuthHeaderMiddleware`` calls this helper on the auth-failure
    path so it can distinguish "this token belongs to an agent that
    was terminated on <date>" from "this token matches nothing" and
    return a more actionable 401 body in the former case.

    Returns
    -------
    ``{"agent_id": str, "status": str, "terminated_at": Optional[str]}``
    when a matching row exists, else ``None``. The shape is JSON-ready
    so the middleware can copy fields straight onto the response body.

    Implementation
    --------------
    Uses the same ``Agent`` ORM model ``get_agent_by_token`` consumes.
    We don't reuse ``get_agent_by_token`` directly because that
    helper returns a dict with eleven columns (including ``token``,
    the bearer itself); the middleware-facing surface returns only
    the three fields the error envelope needs, so accidental leakage
    of internal state into client-facing JSON is structurally
    impossible.

    Failure mode: any DB error returns None (logged inside the
    ORM layer). The middleware then falls through to the
    `invalid_bearer` branch, which is the safe default — we never
    want a transient DB blip to elevate an unknown-token error into
    a wrong "agent terminated" diagnostic.
    """
    if not token:
        return None
    # Local import — `agent_mcp.db.actions.agent_db` pulls in the
    # SQLAlchemy engine which we don't want to load at module-import
    # time for callers that just want ``get_agent_id``.
    from ..db.actions.agent_db import get_agent_by_token

    row = get_agent_by_token(token)
    if row is None:
        return None
    return {
        "agent_id": row.get("agent_id"),
        "status": row.get("status"),
        "terminated_at": row.get("terminated_at"),
    }