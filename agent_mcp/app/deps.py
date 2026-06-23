"""FastAPI dependencies for the per-project backend (Phase 1 PR D).

The per-project FastAPI app sits behind the aiohttp router. When a
dashboard mutation arrives, the router's middleware has already
validated the operator's session cookie and verified project
membership; by the time the request reaches this dep the auth
decision is mostly redundant.

We still re-validate the cookie at the FastAPI layer for two reasons:

  1. Defence in depth — a misconfiguration that bypassed the router
     middleware (e.g. someone exposing the backend Unix socket
     directly) shouldn't accidentally open the dashboard mutation
     surface.
  2. Per-agent bearers (workers / managers) still POST to the backend
     directly and need to authenticate.

retire-system-token Wave 1 (this PR) removed the god-key bearer
paths through this dep. The dep now admits:

  * A valid ``agent_mcp_session`` cookie pointing at a live operator
    session in ``router.db`` (the dashboard path), OR
  * A verified ``X-Agent-MCP-Forwarded-Operator`` header carrying a
    signed operator identity (the router proxies cookie requests
    with this header attached — Wave 2 wires the router side; Wave 1
    ships only the backend verify), OR
  * A per-agent ``Authorization: Bearer <token>`` whose row has
    ``agent_role IN ('manager', 'admin')`` — the post-Wave-1 stand-
    in for the old "admin bearer" admit. Worker tokens are still
    rejected (no privilege escalation from worker to operator-tier
    REST surface; ``tests/test_tokens_endpoint_worker_guard.py``
    pins this).

The previous behaviour was ``verify_token(bearer, "admin")``, which
matched ``token == g.system_token``. That god-key branch is gone;
the surviving "operator-tier bearer" surface is the per-agent
manager-role token, which is a real per-principal credential that
the test harness mints and external admin scripts can mint by
creating a manager agent via ``create_agent``.

The legacy ``body['token']`` / ``?token=<>`` paths still resolve
through ``verify_token`` — they admit the same per-agent manager
bearers, since ``verify_token`` itself is the single source of
truth on "is this a privileged token".
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException, Request

from ..core.auth import verify_token


logger = logging.getLogger(__name__)


SESSION_COOKIE_NAME = "agent_mcp_session"


def _bearer_is_operator_tier(token: str) -> bool:
    """Post-Wave-1 operator-tier bearer check.

    Previously this was ``verify_token(token, "admin")``, which
    matched the god-key ``g.system_token`` bearer. retire-system-token
    Wave 1 dropped that branch from ``verify_token``, so the surviving
    operator-tier bearer surface is the per-agent manager-role
    (or, historically, admin-role) token in the ``agents`` table.

    Returns True iff ``token`` resolves to a row whose ``agent_role``
    is operator-tier. Worker-role tokens return False (no escalation
    to operator-only REST routes via a worker bearer).
    """
    if not token:
        return False
    # ``manager`` role: real per-principal operator-tier credential
    # the harness + ``create_agent`` mint.
    if verify_token(token, "manager"):
        return True
    # Legacy ``admin`` role: pre-Wave-4 agents-table rows still in
    # the wild. ``verify_token(.., "agent")`` admits any active row;
    # we additionally check the role string so we don't grant
    # operator-tier privilege to worker rows.
    from .. import core
    g = core.globals  # type: ignore[attr-defined]
    row = g.active_agents.get(token)
    if isinstance(row, dict) and row.get("agent_role") == "admin":
        return True
    return False


# ── Resolution helpers ────────────────────────────────────────────


def _resolve_session_user(session_id: str) -> dict[str, Any] | None:
    """Look up the operator behind ``session_id``.

    Returns None on missing/expired session or missing user. Catches
    OperationalError so a per-project backend that runs without the
    router (rare — e.g. ad-hoc test harness) doesn't 500 on the
    missing router.db.
    """
    if not session_id:
        return None
    try:
        from ..router import identity

        session = identity.get_session(session_id)
        if session is None:
            return None
        return identity.get_user_by_id(session["user_id"])
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "operator-session resolution failed for session %r; "
            "treating as anonymous", session_id[:8],
        )
        return None


# ── The dep ───────────────────────────────────────────────────────


async def _legacy_body_token(request: Request) -> str | None:
    """Return ``body['token']`` if the body is a JSON object with one.

    Read carefully: FastAPI lets a downstream handler consume the
    body too; we use ``request.body()`` which caches the raw bytes
    on the Request so subsequent reads (from the handler's Pydantic
    parser) see the same payload. Non-JSON or non-dict bodies fall
    back to None.
    """
    try:
        raw = await request.body()
    except Exception:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    token = parsed.get("token")
    return token if isinstance(token, str) else None


async def require_operator_session(request: Request) -> dict[str, Any]:
    """FastAPI dep — admit cookie OR forwarding-header OR operator-bearer.

    On success returns a dict shaped like::

        {"kind": "session", "user": <user-row>}        # cookie path
        {"kind": "forwarding", "operator_id": <str>}   # signed-header
        {"kind": "admin_token", "user": None}          # bearer / body / qs

    On failure raises HTTPException(401).

    Handlers that want just "did this caller authenticate?" don't
    need to inspect the return value; they can wire the dep via
    ``Depends(require_operator_session)`` and trust the 401 path.

    retire-system-token Wave 1 removed the god-key bearer paths;
    the bearer / body-token / query-string admits now consult
    ``_bearer_is_operator_tier`` which checks for a per-agent
    manager-role row in the ``agents`` table. Worker tokens are
    rejected here — see ``tests/test_tokens_endpoint_worker_guard.py``.
    """
    # 1. Cookie path — dashboard auth (Wave 1 of prancy-napping-pie).
    session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
    if session_id:
        user = _resolve_session_user(session_id)
        if user is not None:
            return {"kind": "session", "user": user}

    # 2. Forwarding-header path — retire-system-token Wave 1. The
    #    ``AuthHeaderMiddleware`` already verified the header and
    #    stamped ``g.current_operator``; we read that here rather
    #    than re-verifying so a single source of truth on
    #    verification semantics + key handling holds. If middleware
    #    rejected the header (tamper / expired / wrong key) the
    #    request never reached this dep — the 401 was returned
    #    upstream.
    from ..core import globals as _g

    if _g.current_operator:
        return {"kind": "forwarding", "operator_id": _g.current_operator}

    # 3. Authorization-bearer path — admits per-agent manager-role
    #    (or legacy admin-role) tokens. Worker tokens fall through.
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
        if bearer and _bearer_is_operator_tier(bearer):
            return {"kind": "admin_token", "user": None}

    # 4. Body-token path — backwards-compat (the JSON body's
    #    "token" field). Same operator-tier gate as the bearer path.
    body_token = await _legacy_body_token(request)
    if body_token and _bearer_is_operator_tier(body_token):
        return {"kind": "admin_token", "user": None}

    # 5. Query-string ``?token=<>`` path — same shape, same gate.
    query_token = request.query_params.get("token") if request.query_params else None
    if query_token and _bearer_is_operator_tier(query_token):
        return {"kind": "admin_token", "user": None}

    raise HTTPException(
        status_code=401,
        detail={
            "error": "login_required",
            "message": (
                "Operator session, signed forwarding header, or "
                "operator-tier bearer required."
            ),
        },
    )


def caller_identity(auth: dict[str, Any]) -> str:
    """Map an auth context to an audit-log identifier.

    Cookie-path callers surface as their username; forwarding-header
    callers surface as the operator_id the router carried in. Falls
    back to ``"admin"`` only when none of the above is present
    (defensive — ``require_operator_session`` would have 401'd before
    reaching a handler in that case).
    """
    user = auth.get("user")
    if isinstance(user, dict) and user.get("username"):
        return str(user["username"])
    op_id = auth.get("operator_id")
    if isinstance(op_id, str) and op_id:
        return op_id
    return "admin"


__all__ = [
    "caller_identity",
    "require_operator_session",
    "SESSION_COOKIE_NAME",
]
