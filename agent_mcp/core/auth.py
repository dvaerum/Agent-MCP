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

    Roles:

    * ``"system"`` / ``"admin"`` — historically satisfied by
      ``g.system_token`` (the god-key bearer). retire-system-token
      Wave 1 (this PR) removed that branch entirely. No bearer token
      satisfies these roles anymore; the function returns True iff the
      ``operator_session_active`` ContextVar is set on the current
      request — i.e. the caller proved operator identity at the HTTP
      layer (via session cookie or signed forwarding header). The
      ``token`` argument is ignored on this branch. The legacy
      ``"admin"`` alias is kept so the boolean-return contract is
      preserved for the transition; new code should consult
      ``operator_session_active`` directly.
    * ``"manager"`` — Phase 2 Wave 2a (v5.0.63). After Wave 1 of
      retire-system-token, the system-bearer fallback here is gone;
      only agent tokens whose row in ``agents`` has
      ``agent_role == 'manager'`` are accepted. Worker-role agent
      tokens are rejected.
    * ``"agent"`` — any currently-active agent token. The
      system-bearer-can-act-as-an-agent fallback has been removed.
    """
    # "system" / "admin" used to admit ``g.system_token``; that branch
    # is gone (retire-system-token Wave 1). Per-request operator
    # identity now flows in via the ``operator_session_active``
    # ContextVar (set by the REST seam in ``routes.py`` and by the
    # tool-call helper in ``tests/harness.py`` when the call
    # originated from an operator session or a verified forwarding
    # header). When that contextvar is set we admit the role — the
    # caller has already proved operator-tier identity at the HTTP
    # layer, and a per-tool ``verify_token(token, "admin")`` check
    # is downstream of that proof.
    if required_role in ("system", "admin"):
        # Lazy import — registry imports this module transitively
        # through authorize.py, so a top-level import would create
        # a cycle at startup.
        try:
            from ..tools.registry import operator_session_active

            if operator_session_active.get():
                return True
        except Exception:  # pragma: no cover - defensive
            pass
        return False
    if not token:  # Added a check for empty/None token
        return False
    # "manager" — agent token whose row has agent_role='manager'.
    # Read via agent_repo so a freshly-restored row missing from the
    # in-memory cache still resolves. (Same cache-first contract as
    # get_agent_id; see PR-W2c.)
    if required_role == "manager":
        from .repositories import agent_repo

        row = agent_repo.get_agent_by_token(token)
        if isinstance(row, dict) and row.get("agent_role") == "manager":
            return True
        return False
    # Check active_agents only if it's not None and token is a key
    if required_role == "agent" and g.active_agents and token in g.active_agents:
        return True
    return False

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