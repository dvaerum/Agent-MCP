# Agent-MCP/mcp_template/mcp_server_src/core/auth.py
import secrets
from typing import Any, Dict, Optional

# Import globals that these functions will operate on
from . import globals as g
# No need to import config here as these functions don't directly use it.

# Original location: main.py, lines 852-854
def generate_token() -> str:
    """Generate a secure random token."""
    return secrets.token_hex(16)

# Original location: main.py, lines 856-866
def verify_token(token: str, required_role: str = "agent") -> bool:
    """
    Verify if a token is valid and has the required role.
    Uses global `g.admin_token` and `g.active_agents`.
    """
    if not token: # Added a check for empty/None token
        return False
    if required_role == "admin" and token == g.admin_token:
        return True
    # Check active_agents only if it's not None and token is a key
    if required_role == "agent" and g.active_agents and token in g.active_agents:
        return True
    # Allow admin token to be used for agent roles as well
    if required_role == "agent" and token == g.admin_token:
        return True  # Admins can act as agents
    return False

# Original location: main.py, lines 868-873
def get_agent_id(token: str) -> Optional[str]:
    """
    Get agent ID from token.
    Uses global `g.admin_token` and `g.active_agents`.
    """
    if not token: # Added a check for empty/None token
        return None
    if token == g.admin_token:
        return "admin" # 'admin' is a special agent_id for admin operations
    # Check active_agents only if it's not None and token is a key
    if g.active_agents and token in g.active_agents:
        # Ensure the agent data dictionary has 'agent_id'
        agent_data = g.active_agents[token]
        if isinstance(agent_data, dict) and "agent_id" in agent_data:
            return agent_data["agent_id"]
    return None


def query_agent_status(token: str) -> Optional[Dict[str, Any]]:
    """If `token` matches a row in the `agents` table, return its
    identifying status info; else return None.

    Purpose
    -------
    `verify_token()` only consults the in-memory `g.active_agents`
    map, which is rebuilt on startup from rows whose
    `status != 'terminated'`. A bearer for a terminated agent
    therefore fails `verify_token()` and is indistinguishable from a
    freshly-invented unknown token — both produce the same generic
    401.

    `AuthHeaderMiddleware` calls this helper on the auth-failure
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
    Uses the `Agent` ORM model from PR-G2 — same model
    `get_agent_by_token` consumes. We don't reuse `get_agent_by_token`
    directly because that helper returns a dict with eleven columns
    (including `token`, the bearer itself); the middleware-facing
    surface returns only the three fields the error envelope needs,
    so accidental leakage of internal state into client-facing JSON
    is structurally impossible.

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
    # time for callers that just want `verify_token`/`get_agent_id`.
    from ..db.actions.agent_db import get_agent_by_token

    row = get_agent_by_token(token)
    if row is None:
        return None
    return {
        "agent_id": row.get("agent_id"),
        "status": row.get("status"),
        "terminated_at": row.get("terminated_at"),
    }