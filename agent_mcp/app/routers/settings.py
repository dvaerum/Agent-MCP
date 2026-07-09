"""Settings router — non-CRUD, router-config-shaped endpoints.

Wave 8 PR 1 of prancy-napping-pie: the configuration-shaped reads
mechanically moved out of ``app/routes.py`` onto this router:
``tokens``, ``aoe_health``, ``prompts_catalog``.

The router uses the bare ``/api`` prefix (rather than a
per-resource sub-prefix) and each handler registers its own
full path (e.g. ``/aoe/health`` → mounted at ``/api/aoe/health``).

Auth: handler-level ``Depends(require_operator_session)`` is kept
verbatim on the handlers that currently have it. The router-level
gate is deferred to a follow-up PR — ``GET /api/prompts/catalog``
is currently open today and hoisting the gate to the router would
silently flip its auth behavior, which is out of scope for a
mechanical URL-stable move.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import handle_options
from ..deps import require_operator_session
from .composition import is_confirmed_operator_tier
from ...core.config import logger
from ...core import globals as g


router = APIRouter(
    prefix="/api",
    tags=["settings"],
)


# SD-R16-1: ``GET /api/aoe/health`` is reachable by a viewer-tier operator
# (``require_operator_session`` admits viewers on GET — the per-project
# backend cannot resolve the caller's project role). ``check_health``'s
# ``message`` strings embed the internal AoE ``base_url`` (topology
# disclosure) and raw httpx/exception text, and the "ok" path returns
# ``base_url`` outright. Sanitise at the response boundary so internal
# callers of ``check_health`` (and the server log) keep the detail while
# the client sees only a coarse, useful status. Whitelisting the output
# fields (rather than blacklisting the leaky ones) keeps this robust
# against any future leaky field on the ``check_health`` result.
_AOE_HEALTH_CLIENT_MESSAGES = {
    "ok": "AoE reachable",
    "disabled": "AoE notifications are disabled",
    "unauthorized": "AoE rejected the bearer token (missing, stale, or rejected)",
    "unreachable": "AoE is unreachable",
    "timeout": "AoE timed out",
    "misconfigured": "AoE bearer-token source is misconfigured (see server log)",
}


def _sanitize_aoe_health(result: dict) -> dict:
    """Strip internal detail from a ``check_health`` result before it
    crosses the client boundary. See SD-R16-1.

    Keeps the coarse ``status`` (+ ``session_count`` when present) an
    operator needs; replaces the detailed ``message`` with a static
    per-status string and drops ``base_url`` and any raw exception text.
    """
    status = result.get("status", "unreachable")
    out: dict = {
        "status": status,
        "message": _AOE_HEALTH_CLIENT_MESSAGES.get(status, "AoE status unknown"),
    }
    session_count = result.get("session_count")
    if isinstance(session_count, int):
        out["session_count"] = session_count
    return out


@router.api_route("/tokens", methods=["GET", "OPTIONS"])
async def tokens_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """GET /api/tokens — dashboard's source of agent bearer tokens.

    Wave 1 (prancy-napping-pie): cookie-migrated. The dep accepts:

      * an ``agent_mcp_session`` cookie pointing at a live operator
        session (the new dashboard path), OR
      * an ``Authorization: Bearer <manager-agent-token>`` header
        (operator-tier per-agent bearer; legacy admin-scripts and the
        agent CLI), OR
      * the same token in a body / query-string field (oldest
        backwards-compat path; nothing in the dashboard sends this).

    The manual ``Authorization: Bearer`` worker-rejection ladder that
    used to live here is now superseded by the dep — any non-operator-
    tier bearer fails ``_bearer_is_operator_tier`` and the request
    401s before reaching this handler.

    Wave 3 (prancy-napping-pie) dropped the legacy ``admin_token``
    field from the response. The dashboard no longer reads it (Wave 2
    stripped the frontend reads). Out-of-tree clients that POST'd to
    this endpoint expecting an ``admin_token`` field must migrate to
    per-agent bearer tokens; see ``docs/external-mcp-client.md`` for
    the provisioning walkthrough. retire-system-token Wave 3 deleted
    the underlying god-key; there is no equivalent value to surface.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    # SECURITY: this endpoint returns the full agent bearer-token list.
    # ``require_operator_session`` admits viewer-tier operators via the
    # cookie/forwarding path on GET (the router only gates mutations on
    # tier, and the backend cannot resolve the caller's project role),
    # so a read-only viewer could otherwise harvest every agent's bearer
    # and replay it to escalate to write. Only a CONFIRMED operator-tier
    # bearer may read the list; everything else gets 403. See
    # ``is_confirmed_operator_tier``.
    if not is_confirmed_operator_tier(auth):
        return JSONResponse(
            {
                "error": "forbidden",
                "message": (
                    "Agent bearer tokens are operator-tier only. Use an "
                    "operator-tier bearer (agent CLI / admin script) to "
                    "read this endpoint."
                ),
            },
            status_code=403,
        )
    try:
        agent_tokens_list = []
        for token, data in g.active_agents.items():
            if data.get("status") != "terminated":
                agent_tokens_list.append({"agent_id": data.get("agent_id"), "token": token})
        return JSONResponse({"agent_tokens": agent_tokens_list})
    except Exception as e:
        logger.error(f"Error retrieving tokens for dashboard: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — ``str(e)`` on a
        # SQLAlchemyError / sqlite3.*Error embeds SQL text + bound
        # params (schema disclosure). Detail stays in the log above.
        return JSONResponse({"error": "Error retrieving tokens"}, status_code=500)


@router.api_route("/aoe/health", methods=["GET", "OPTIONS"])
async def aoe_health_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """GET /api/aoe/health — admin-only AoE-reachability probe.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    The dashboard no longer passes the admin token in the query string.

    Pings the configured AoE instance with the current bearer token
    (resolved live, including file-sourced rotations). The raw
    ``check_health`` result is sanitised through ``_sanitize_aoe_health``
    (SD-R16-1) before it crosses the client boundary, so the response
    carries only a coarse status + static message:

      {"status": "ok",            "session_count": N, "message": "..."}
      {"status": "disabled",      "message": "..."}
      {"status": "unauthorized",  "message": "..."}
      {"status": "unreachable",   "message": "..."}
      {"status": "misconfigured", "message": "..."}

    The internal AoE ``base_url`` and any httpx/exception text stay in
    the server log, never in the client body (the endpoint is
    viewer-reachable — see ``_sanitize_aoe_health``).

    Used by the Settings tab to surface a "your AoE token has gone
    stale" warning without requiring the admin to attempt a real send.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'GET':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    from ...features.aoe_notify import check_health

    try:
        result = await check_health()
    except Exception as e:
        logger.error("AoE health probe crashed: %s", e, exc_info=True)
        # SD-R16-1: static client message — ``str(e)`` on the outbound
        # httpx/probe failure embeds the internal AoE ``base_url`` +
        # exception text. Detail stays in the log above.
        return JSONResponse(
            {
                "status": "unreachable",
                "message": _AOE_HEALTH_CLIENT_MESSAGES["unreachable"],
            },
            status_code=200,
        )
    return JSONResponse(_sanitize_aoe_health(result))


# --- Prompt Book catalog (plan Phase 6) ---
@router.api_route("/prompts/catalog", methods=["GET", "OPTIONS"])
async def prompts_catalog_api_route(request: Request) -> JSONResponse:
    """`GET /api/prompts/catalog` — the single source of truth
    for the Prompt Book catalogue.

    Sourced from `agent_mcp/prompts/catalog.json` so MCP
    `prompts/list` and the dashboard read the same data.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    from ...prompts import load_catalog
    return JSONResponse(load_catalog())


# --- Catch-all OPTIONS handler ---
# Mirrors the legacy `('/api/{path:path}', handle_options, ['OPTIONS'], None)`
# entry at the bottom of `_dashboard_route_specs`. Registered last
# (settings is included after every other `/api`-prefix router) so
# the per-route OPTIONS registrations on agents/tasks/memories/messages
# still win the match. See `routers/__init__.py` for the include order.
@router.options("/{path:path}")
async def options_catch_all(request: Request, path: str):
    return await handle_options(request)
