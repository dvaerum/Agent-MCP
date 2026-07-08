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
     surface. The cookie path therefore AUTHORIZES, not just
     authenticates: it reverse-maps this backend's ``MCP_PROJECT_DIR``
     to its project name via the router registry and re-resolves the
     caller's membership + operator/viewer split with the same
     resolver the router uses (see ``_authorize_session_for_project``).
     A cookie for a project the operator isn't a member of, or a
     viewer attempting a mutation, is rejected — even if the backend
     is reached directly. When the project name can't be determined
     (ad-hoc / test harness) the dep falls back to authenticate-only.
  2. Per-agent bearers (workers / managers) still POST to the backend
     directly and need to authenticate.

The dep admits:

  * A valid ``agent_mcp_session`` cookie pointing at a live operator
    session in ``router.db`` (the dashboard path), OR
  * A verified ``X-Agent-MCP-Forwarded-Operator`` header carrying a
    signed operator identity (the router proxies cookie requests
    with this header attached), OR
  * A per-agent ``Authorization: Bearer <token>`` whose row has
    ``agent_role IN ('manager', 'admin')`` — Worker tokens are
    rejected (no privilege escalation from worker to operator-tier
    REST surface; ``tests/test_tokens_endpoint_worker_guard.py``
    pins this).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request


logger = logging.getLogger(__name__)


SESSION_COOKIE_NAME = "agent_mcp_session"


#: HTTP methods that mutate. Kept in lock-step with the router's
#: ``auth_middleware._MUTATION_METHODS`` so the backend's cookie-path
#: operator/viewer split matches the wire gate exactly: reads (GET /
#: HEAD / OPTIONS) admit on either tier; these require operator.
_MUTATION_METHODS = frozenset({"POST", "PATCH", "DELETE", "PUT"})


#: Agent rows whose ``agent_role`` is treated as operator-tier for
#: the bearer / body-token / query-string admit paths below. The
#: post-Wave-1 ``manager`` role is the canonical credential; the
#: legacy ``admin`` role is kept for pre-Wave-4 rows still in the wild.
_OPERATOR_TIER_AGENT_ROLES = frozenset({"manager", "admin"})


def _is_operator_tier_bearer(token: str) -> bool:
    """Return True iff ``token`` resolves to an operator-tier agent row.

    Direct DB / cache lookup against ``agent_role`` — Wave 6 PR 6
    retired the ``verify_token`` indirection. Worker-role tokens
    return False (no escalation from worker to operator-tier REST
    routes via a worker bearer).
    """
    if not token:
        return False
    from ..core.repositories import agent_repo
    row = agent_repo.get_agent_by_token(token)
    if not isinstance(row, dict):
        return False
    return row.get("agent_role") in _OPERATOR_TIER_AGENT_ROLES


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


def _backend_project_name() -> str | None:
    """Best-effort: the project name THIS backend serves, or None.

    The systemd launcher hands the backend only ``--project-dir`` (→
    ``MCP_PROJECT_DIR``); the project *name* lives in the router-owned
    registry keyed name→workspace. Reverse-map our resolved project dir
    against the registry so the cookie-path authorization gate can ask
    the router's resolver "is this caller a member of THIS project?".

    Returns None when the registry is unavailable or has no entry whose
    workspace matches our dir (ad-hoc / test harness, or a
    not-yet-registered project). A None here means the dep cannot
    authorize by project and falls back to the pre-existing
    authenticate-only behaviour — no worse than before the fix, and it
    keeps the fix from denying deploys where the reverse-map is
    genuinely unavailable. In the internet-exposure posture this fix
    targets (co-located systemd backend reachable directly) the
    registry IS present, so the hole is closed where it matters.
    """
    try:
        from ..core import config
        from ..router.project_registry import ProjectRegistry

        my_dir = config.get_project_dir()
        for proj in ProjectRegistry().list():
            workspace = proj.get("workspace")
            if not workspace:
                continue
            try:
                if Path(workspace).resolve() == my_dir:
                    return proj.get("name")
            except OSError:  # pragma: no cover - defensive (unresolvable path)
                continue
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def _authorize_session_for_project(user: dict[str, Any], request: Request) -> None:
    """Enforce project membership + the read/mutation split on the
    cookie/session path — the AUTHORIZE step the dep previously skipped.

    Mirrors ``router/auth_middleware.require_operator_session_middleware``:

      * sysadmin (directly or via a group) bypasses the membership check;
      * a caller with no role for this backend's project → 401;
      * a viewer performing a mutation (POST/PATCH/PUT/DELETE) → 403;
      * everyone else admits.

    The normal router-proxied path is unaffected: the router already
    gated membership with the same resolver before forwarding the
    cookie, so this re-resolves the identical role and admits. When the
    backend cannot determine its own project name, we return without
    enforcing (see :func:`_backend_project_name`).

    Raises ``HTTPException`` (401 / 403) on denial.
    """
    project = _backend_project_name()
    if project is None:
        return

    user_id = user.get("user_id")
    if user_id is None:  # pragma: no cover - session rows always carry one
        return
    user_id = str(user_id)

    from ..router import group_resolver

    try:
        if group_resolver.resolve_user_is_sysadmin(user_id):
            return
    except Exception:  # pragma: no cover - defensive; mirror router (non-sysadmin)
        pass

    try:
        role = group_resolver.resolve_user_project_role(user_id, project)
    except sqlite3.OperationalError:
        # router.db missing/unmigrated — but reaching here means the
        # session already resolved against the same DB, so this is
        # unexpected. Fail closed (role=None → deny), consistent with
        # the router middleware.
        role = None
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "backend project-role resolution failed for user=%r project=%r",
            user.get("username"), project,
        )
        role = None

    if role is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "login_required",
                "message": (
                    f"operator {user.get('username')!r} has no membership "
                    f"in project {project!r}"
                ),
            },
        )

    if request.method.upper() in _MUTATION_METHODS and role != "operator":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": (
                    f"viewer-tier operator {user.get('username')!r} "
                    f"cannot mutate project {project!r}"
                ),
            },
        )


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

        {"kind": "session", "user": <user-row>}             # cookie path
        {"kind": "forwarding", "operator_id": <str>}        # signed-header
        {"kind": "operator_bearer", "user": None}           # bearer / body / qs

    On failure raises HTTPException(401).

    The ``"operator_bearer"`` discriminator was named ``"admin_token"``
    before retire-system-token Wave 5; it never carried a god-key
    admin token after Wave 1 (it admits per-agent manager-role tokens
    via ``_is_operator_tier_bearer``), so the legacy name was
    misleading. The discriminator is internal — no handler branches on
    it post-Wave-3.

    Handlers that want just "did this caller authenticate?" don't
    need to inspect the return value; they can wire the dep via
    ``Depends(require_operator_session)`` and trust the 401 path.

    retire-system-token Wave 1 removed the god-key bearer paths;
    the bearer / body-token / query-string admits now consult
    ``_is_operator_tier_bearer`` which checks for a per-agent
    manager-role row in the ``agents`` table. Worker tokens are
    rejected here — see ``tests/test_tokens_endpoint_worker_guard.py``.
    """
    # 1. Cookie path — dashboard auth (Wave 1 of prancy-napping-pie).
    session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
    if session_id:
        user = _resolve_session_user(session_id)
        if user is not None:
            # AUTHORIZE, not just authenticate: re-check project
            # membership + the read/mutation split for THIS backend's
            # project so a cookie for another project (or a viewer
            # mutating) can't walk in when the backend is reached
            # directly. Raises 401/403 on denial.
            _authorize_session_for_project(user, request)
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
        if bearer and _is_operator_tier_bearer(bearer):
            return {"kind": "operator_bearer", "user": None}

    # 4. Body-token path — backwards-compat (the JSON body's
    #    "token" field). Same operator-tier gate as the bearer path.
    body_token = await _legacy_body_token(request)
    if body_token and _is_operator_tier_bearer(body_token):
        return {"kind": "operator_bearer", "user": None}

    # 5. Query-string ``?token=<>`` path — same shape, same gate.
    query_token = request.query_params.get("token") if request.query_params else None
    if query_token and _is_operator_tier_bearer(query_token):
        return {"kind": "operator_bearer", "user": None}

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
