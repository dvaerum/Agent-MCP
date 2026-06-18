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
  2. The legacy ``Authorization: Bearer <admin_token>`` path stays
     valid for operators with admin-token scripts that POST to the
     backend directly. Dropping this would break every existing
     integration (and every existing test) overnight.

So the dep admits ANY of:

  * A valid ``agent_mcp_session`` cookie pointing at a live
    operator session in ``router.db`` (the new dashboard path), OR
  * An ``Authorization: Bearer <admin_token>`` header where the
    bearer matches the per-project admin token (legacy backwards-
    compat for admin scripts + the test harness), OR
  * The legacy ``token`` field in the JSON body matching the
    admin token (oldest backwards-compat path; the dashboard no
    longer sends this, but pre-PR-D integrations and tests do).

Failure to provide any of the above on a route guarded by
``require_operator_session`` returns 401.

Phase 2 will replace the third path with a deprecation warning, then
remove it; for Phase 1 we accept all three to avoid breaking the
~58 existing tests that POST ``{"token": admin_token, ...}``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException, Request

from ..core.auth import verify_token


logger = logging.getLogger(__name__)


SESSION_COOKIE_NAME = "agent_mcp_session"


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


# ── The dep ───────────────────────────────────────────────────────


async def require_operator_session(request: Request) -> dict[str, Any]:
    """FastAPI dep — admit cookie OR Authorization-Bearer OR body-token.

    On success returns a dict shaped like::

        {"kind": "session", "user": <user-row>}      # cookie path
        {"kind": "admin_token", "user": None}        # legacy bearer
        {"kind": "admin_token", "user": None}        # legacy body-token

    On failure raises HTTPException(401).

    Handlers that want just "did this caller authenticate?" don't
    need to inspect the return value; they can wire the dep via
    ``Depends(require_operator_session)`` and trust the 401 path.

    The legacy paths are tagged ``"admin_token"`` so audit-log
    middleware (future PR) can differentiate. The dashboard never
    sends the legacy paths post-PR-D.
    """
    # 1. Cookie path — new dashboard auth.
    session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
    if session_id:
        user = _resolve_session_user(session_id)
        if user is not None:
            return {"kind": "session", "user": user}

    # 2. Authorization-bearer path — legacy admin scripts / agents.
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
        if bearer and verify_token(bearer, required_role="admin"):
            return {"kind": "admin_token", "user": None}

    # 3. Body-token path — oldest backwards-compat (the JSON body's
    #    "token" field). The dashboard no longer sends this post-PR-D;
    #    the per-handler grep test asserts no handler READS the
    #    body's token, but the dep reading it preserves the auth
    #    decision for legacy test/script callers.
    body_token = await _legacy_body_token(request)
    if body_token and verify_token(body_token, required_role="admin"):
        return {"kind": "admin_token", "user": None}

    # 4. Query-string ``?token=<>`` path — also backwards-compat. The
    #    ``GET /api/agents/<id>/purge-preview`` endpoint historically
    #    took the admin token in the query string (browsers strip GET
    #    bodies per Fetch spec). The dashboard no longer sends this
    #    post-PR-D, but pre-PR-D scripts + tests do.
    query_token = request.query_params.get("token") if request.query_params else None
    if query_token and verify_token(query_token, required_role="admin"):
        return {"kind": "admin_token", "user": None}

    raise HTTPException(
        status_code=401,
        detail={
            "error": "login_required",
            "message": (
                "Operator session, admin-bearer header, or admin-token "
                "body field required."
            ),
        },
    )


def caller_identity(auth: dict[str, Any]) -> str:
    """Map an auth context to an audit-log identifier.

    Cookie-path callers surface as their username; admin-token-path
    callers surface as the literal ``"admin"`` (matching the
    pre-PR-D behaviour, which used the admin token to look up the
    Admin agent_id).

    Used by handlers that record ``log_agent_action_to_db`` so the
    audit log shows the operator's name instead of an empty string.
    """
    user = auth.get("user")
    if isinstance(user, dict) and user.get("username"):
        return str(user["username"])
    return "admin"


__all__ = [
    "caller_identity",
    "require_operator_session",
    "SESSION_COOKIE_NAME",
]
