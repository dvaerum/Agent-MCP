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

    Uses globals ``g.system_token`` (the router-internal authority
    bearer, formerly known as ``admin_token``) and ``g.active_agents``.

    Roles:

    * ``"system"`` — only the router-internal authority bearer is
      accepted. This is the canonical role name introduced by Phase 2
      Wave 1b's rename.
    * ``"admin"`` — deprecated alias for ``"system"``. Kept for one
      release so existing per-tool ``verify_token(token, "admin")``
      sites continue to work; new code should use ``"system"``.
    * ``"manager"`` — Phase 2 Wave 2a (v5.0.63). Accepts the system
      bearer OR an agent token whose row in ``agents`` has
      ``agent_role == 'manager'``. Worker-role agent tokens are
      rejected. Used by ``@requires_role("manager")`` to gate
      supervision-tier tools (assign-task to other agents, edit
      subordinate agent metadata) without granting operator-tier
      powers (spawn/terminate agents, mutate ``config_*`` keys).
    * ``"agent"`` — any currently-active agent token, or the system
      bearer (which can act as an agent).
    """
    if not token: # Added a check for empty/None token
        return False
    # Treat "admin" as a deprecated alias for "system" — see docstring.
    if required_role in ("system", "admin") and token == g.system_token:
        return True
    # "manager" — system bearer OR an agent token whose row has
    # agent_role='manager'. Read via agent_repo so a freshly-restored
    # row missing from the in-memory cache still resolves. (Same
    # cache-first contract as get_agent_id; see PR-W2c.)
    if required_role == "manager":
        if token == g.system_token:
            return True
        from .repositories import agent_repo

        row = agent_repo.get_agent_by_token(token)
        if isinstance(row, dict) and row.get("agent_role") == "manager":
            return True
        return False
    # Check active_agents only if it's not None and token is a key
    if required_role == "agent" and g.active_agents and token in g.active_agents:
        return True
    # Allow the system bearer to be used for agent roles as well.
    if required_role == "agent" and token == g.system_token:
        return True  # The system bearer can act as an agent.
    return False

# Original location: main.py, lines 868-873
def get_agent_id(token: str) -> Optional[str]:
    """
    Get agent ID from token.
    Uses global ``g.system_token`` (Phase 2 Wave 1b rename of the
    legacy ``admin_token``) and the AgentRepository (cache-first
    lookup; falls through to the DB on miss).

    Migrated to ``agent_repo.get_agent_by_token`` in PR-W2c so a token
    for a row that's only in the DB (e.g. just restored by a peer
    process) resolves correctly without waiting for the next lifespan
    reload. The repo keeps the cache-hit semantics for the common
    case via ``state.active_agents``.
    """
    if not token: # Added a check for empty/None token
        return None
    if token == g.system_token:
        return "admin" # 'admin' is a special agent_id for the system bearer's actions
    # Local import to keep the legacy module-load contract: callers
    # that only want verify_token/get_agent_id shouldn't pay the cost
    # of loading the SQLAlchemy engine until the first DB-miss path.
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