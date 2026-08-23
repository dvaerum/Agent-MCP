"""Shared REST→tool dispatch helpers for the per-resource APIRouters.

Wave 8 PR 1 of prancy-napping-pie lifted these helpers out of
``agent_mcp/app/routes.py`` so the per-resource router modules under
``agent_mcp/app/routers/`` can share them without depending on the
shrinking back-compat shim. Bodies are unchanged from their previous
home in ``routes.py``; only the import path moved.

Exports:
  * :func:`_dispatch_through_tool` — wrap an MCP tool call as a
    dashboard JSON response, with the typed-ToolResult → HTTP mapping
    the dashboard's ApiClient already consumes.
  * :func:`_build_route_principal` — construct the Principal the REST
    seam threads into the dispatcher (operator-session OR agent-bearer
    shapes).
  * :func:`_result_text` — concatenate the text blocks from a
    tool-call result (legacy helper kept for the few handlers that
    still consult it).
  * :func:`handle_options` — CORS preflight reply used by handlers
    that include ``OPTIONS`` in their methods list.

The PR 2 cleanup will delete ``routes.py``; this module survives
because the routers genuinely share it.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.requests import Request

from ..core.authorize import AuthRejected
from ..core.config import logger
from ..core.principal import Principal
from ..core.tool_result import (
    Failed as _Failed,
    Ok as _Ok,
    tool_result_to_http,
)
from ..tools.registry import (
    ToolInputValidationError,
    dispatch_tool_call,
    request_auth_token,
)

import mcp.types as mcp_types  # For handling the result from tool_impl


def _result_text(result: List[mcp_types.TextContent]) -> str:
    """Concatenate text blocks from a tool-call result."""
    if not result:
        return ""
    parts: List[str] = []
    for block in result:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


async def _dispatch_through_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    bearer_token: Optional[str],
    success_message: Optional[str] = None,
    extra_response: Optional[Dict[str, Any]] = None,
    operator_session: bool = False,
    operator_user_id: Optional[str] = None,
) -> JSONResponse:
    """Run an MCP tool from a REST handler and translate the typed
    :data:`ToolResult` back into a dashboard-friendly JSON response.

    Auth: the dashboard sends the admin token in the JSON body, not as
    an Authorization header. We bind it on the ``request_auth_token``
    ContextVar so the dispatcher's Q6e fallback injects it into
    ``arguments.token`` if not already there — same path an HTTP
    middleware would take.

    Callers pass ``operator_session=True`` + ``operator_user_id=<username>``
    to admit operator-tier dispatches without a bearer token. We build
    a ``Principal`` from those values and thread it to the dispatcher;
    when neither is set, the bearer is expected to carry an
    ``agent_bearer`` Principal (built locally from the row).

    Wave 6 PR 6: the legacy ContextVar stamp block (which set the
    deleted ``operator_session_active`` / ``operator_user_id`` vars
    so unmigrated decorators could read them) is gone. The Principal
    is the single carrier of caller identity; tool decorators read
    it directly.

    Error mapping (HTTP-shaped):
      * AuthRejected            → 403 (raised, before dispatch)
      * ToolInputValidationError → 400 (raised, before dispatch)
      * Unexpected exception    → 500 (raised)
      * ToolResult error variants → via the shared adapter
        :func:`agent_mcp.core.tool_result.tool_result_to_http`
        (NotFound 404, PermissionDenied 403, Invalid 400, Conflict
        409, Failed 500).

    Success payload mirrors the legacy REST endpoints'
    ``{"success": true, "message": "...", ...extras}`` shape so the
    dashboard's ApiClient doesn't have to change.
    """
    cv_token = None
    if bearer_token:
        cv_token = request_auth_token.set(bearer_token)

    dispatch_principal = _build_route_principal(
        bearer_token=bearer_token,
        operator_session=operator_session,
        operator_user_id=operator_user_id,
    )
    try:
        result = await dispatch_tool_call(
            tool_name, arguments, principal=dispatch_principal,
        )
    except AuthRejected as e:
        return JSONResponse(
            {"success": False, "error": e.reason, "message": e.reason},
            status_code=403,
        )
    except ToolInputValidationError as e:
        return JSONResponse(
            {"success": False, "error": str(e), "message": str(e)},
            status_code=400,
        )
    except Exception as e:
        # SD-R7-1: the raw ``str(e)`` of an uncaught tool-body exception
        # (sqlite3/SQLAlchemy error, KeyError, OSError with a path) leaks
        # table/column names, filesystem paths, and internals. Log the
        # detail server-side only; return a STATIC generic 500 message.
        logger.error(
            f"Unexpected error dispatching tool {tool_name!r}: {e}",
            exc_info=True,
        )
        return JSONResponse(
            {
                "success": False,
                "error": "Tool dispatch failed",
                "message": "Tool dispatch failed",
            },
            status_code=500,
        )
    finally:
        if cv_token is not None:
            request_auth_token.reset(cv_token)

    if not isinstance(result, _Ok):
        # Every error variant → HTTP via the ONE shared adapter
        # (:func:`agent_mcp.core.tool_result.tool_result_to_http`). The
        # adapter owns the variant→status table + the canonical dashboard
        # body; this replaced the inline ladder that used to live here and
        # had drifted from the register-agent route's copy (PermissionDenied
        # 403-vs-401). The status source is now single.
        if isinstance(result, _Failed):
            # SEC-R8-1: ~40 tool impls return ``Failed(message=f"…{e}")``
            # built from a caught sqlite3/SQLAlchemy error, so
            # ``result.message`` can embed table/column names, filesystem
            # paths, and internals. The adapter emits a STATIC generic
            # client message; log the real detail (with the tool name for
            # server-side triage) here where the tool_name is in scope.
            logger.error(
                f"Tool {tool_name!r} returned Failed result: {result.message}"
            )
        status, body = tool_result_to_http(result)
        return JSONResponse(body, status_code=status)

    # ``Ok`` — success. ``Ok.data`` (when set) is the JSON body the
    # dashboard consumes; ``Ok.message`` provides a human-readable
    # summary the success-banner reads when the caller didn't supply
    # one via ``success_message``.
    ok_payload: Dict[str, Any] = {
        "success": True,
        "message": success_message or (result.message or ""),
    }
    if result.data is not None:
        ok_payload["data"] = result.data
    if extra_response:
        ok_payload.update(extra_response)
    # 201 heuristic: create_* tools naming convention. Refine if a
    # future tool needs different semantics.
    status = 201 if (
        tool_name.startswith("create_") and result.data is not None
    ) else 200
    return JSONResponse(ok_payload, status_code=status)


def _build_route_principal(
    *,
    bearer_token: Optional[str],
    operator_session: bool,
    operator_user_id: Optional[str],
    project_name: Optional[str] = None,
) -> Optional[Principal]:
    """Construct the Principal the REST seam threads into the dispatcher.

    Three shapes:

    * ``operator_session=True`` → ``operator_session`` Principal
      naming the operator (the dashboard / forwarding-header path).
    * Bearer present, no operator session → ``agent_bearer`` Principal
      sourced from the row in ``agents``.
    * Neither → None (the dispatcher will reject downstream).

    AC-R5-1 (round 5): for the ``operator_session`` shape, use the
    forwarding caller's REAL HMAC-signed ``project_role`` + ``sysadmin``
    when :func:`agent_mcp.app.deps.forwarding_route_role` reports them
    (set by ``require_operator_session``'s forwarding branch for THIS
    request task). A forwarding VIEWER thus gets a viewer-role Principal
    whose capability set the tool's own gate denies — closing the latent
    viewer→operator escalation the hard-coded ``"operator"`` left open.
    The cookie / operator-tier bearer paths report ``None`` here and keep
    the historical operator-tier default: those paths are genuinely
    operator (the cookie mutation admit is authorized as operator
    upstream; the bearer resolves an operator-tier agent row).

    Finding B (security-arch-hardening-consolidated.md Phase 1):
    ``project_name`` is best-effort plumbing for routes whose tool call
    needs it explicitly (e.g. ``agents.register`` — the per-project
    backend doesn't yet derive its own project name from the request).
    Every other call site omits it and keeps the historical ``None``.
    """
    from ..core.principal_builder import (
        build_agent_bearer_principal,
        build_operator_principal,
    )

    if operator_session:
        # Local import: deps.py imports nothing from this module, so a
        # module-level import is cycle-free — but keeping it local also
        # sidesteps any app-construction import-ordering surprise.
        from .deps import forwarding_route_role

        threaded = forwarding_route_role()
        if threaded is not None:
            project_role, sysadmin = threaded
        else:
            project_role, sysadmin = "operator", False
        # arch-B: build via the shared builder so caps resolve through the
        # one code path (this used to lean on Principal.__post_init__'s
        # back-fill; the builder now resolves explicitly, identically).
        return build_operator_principal(
            user_id=operator_user_id,
            kind="operator_session",
            project_role=project_role,
            sysadmin=sysadmin,
            project_name=project_name,
            source_token=bearer_token,
        )
    if bearer_token:
        # arch-B: the ×4-duplicated agent_bearer block now lives once in
        # ``core.principal_builder``. No wake-loop lookup on the REST path.
        return build_agent_bearer_principal(bearer_token)
    return None


# VULN-001 (security audit 2026-06-29): allowed browser origins for the
# dashboard + API surface. Single source of truth shared between the
# CORSMiddleware in :mod:`agent_mcp.app.main_app` and the per-route
# :func:`handle_options` fallback below.
#
# Wildcard (``*``) is intentionally absent — pairing it with
# ``Access-Control-Allow-Credentials: true`` makes any browser at any
# origin able to issue credentialed requests, which lets a logged-in
# operator be CSRF'd from an attacker-controlled page.
#
# SEC-1 fold-in (2026-07): the production default is now EMPTY. Shipping
# ``localhost:3000/3001/3847`` in the default allowlist paired with
# ``allow_credentials=True`` is a latent CSRF surface on any deployment
# that binds a reachable interface — a malicious app running on the
# victim's own machine at one of those origins could issue credentialed
# requests against the dashboard using the operator's session cookie.
# Local dashboard development opts the dev origins back in via the
# existing ``MCP_DASHBOARD_EXTRA_ORIGINS`` env-var (see
# :data:`_DEV_ORIGINS` for the exact value to set).
_DEFAULT_ALLOWED_ORIGINS: frozenset[str] = frozenset()

#: The localhost origins the local dashboard dev servers run on (Next.js
#: dev on :3000/:3001, the packaged dashboard on :3847). NOT included by
#: default — a developer opts them in explicitly:
#:
#:     export MCP_DASHBOARD_EXTRA_ORIGINS=\
#:       "http://localhost:3000,http://localhost:3001,\
#:        http://localhost:3847,http://127.0.0.1:3847"
#:
#: Kept as a named constant so the value has one canonical home the
#: docs / dev tooling can reference instead of hard-coding the list.
_DEV_ORIGINS: frozenset[str] = frozenset({
    'http://localhost:3847',
    'http://127.0.0.1:3847',
    'http://localhost:3000',
    'http://localhost:3001',
})


def _load_extra_origins() -> frozenset[str]:
    """Read ``MCP_DASHBOARD_EXTRA_ORIGINS`` and return the parsed set.

    Audit-A INFO-003 (2026-06-30): operators serving the dashboard
    behind a reverse proxy (tailnet, custom domain) need a way to
    extend the CORS allowlist without editing the source. The env-var
    accepts a comma-separated list of full origins (scheme included):

        MCP_DASHBOARD_EXTRA_ORIGINS="https://dashboard.example.com,\
        https://ops.internal"

    Explicit ``'*'`` is rejected at load time — the whole point of the
    VULN-001 fix was that ``allow_credentials=True`` paired with a
    wildcard is CSRF-shaped. Refusing the wildcard here prevents an
    operator hitting a CORS error under pressure and "fixing" it by
    setting the env-var to ``*``.

    Missing scheme is also rejected (``evil.com`` → ValueError). Full
    origins have to be spelled out so there's no ambiguity between
    matching ``http://evil.com`` and ``https://evil.com``.
    """
    raw = os.environ.get("MCP_DASHBOARD_EXTRA_ORIGINS", "")
    if not raw:
        return frozenset()
    origins = frozenset(o.strip() for o in raw.split(",") if o.strip())
    for origin in origins:
        if origin == "*":
            raise ValueError(
                "MCP_DASHBOARD_EXTRA_ORIGINS does not accept '*' — "
                "wildcard CORS with credentials is a security "
                "vulnerability (VULN-001). List explicit origins "
                "instead."
            )
        if not (origin.startswith("http://") or origin.startswith("https://")):
            raise ValueError(
                f"MCP_DASHBOARD_EXTRA_ORIGINS entry {origin!r} must be "
                "a full origin including scheme (http:// or https://)."
            )
    return origins


ALLOWED_ORIGINS: frozenset[str] = (
    _DEFAULT_ALLOWED_ORIGINS | _load_extra_origins()
)


async def handle_options(request: Request) -> Response:
    """Handle OPTIONS requests for CORS preflight.

    Falls through for origins not in :data:`ALLOWED_ORIGINS`: returns
    an empty 200 with no ``Access-Control-Allow-*`` headers so the
    browser rejects the preflight. The previous wildcard reply paired
    with ``Allow-Credentials: true`` on the surrounding middleware
    let attacker origins satisfy preflight for credentialed requests
    (VULN-001).

    In normal operation Starlette's CORSMiddleware short-circuits
    preflight for allowed origins before the request reaches this
    handler — this code path is the fallback for non-allowed origins
    and for routes that opt into ``OPTIONS`` in their methods list.
    """
    origin = request.headers.get('origin', '')
    if origin not in ALLOWED_ORIGINS:
        # Don't echo CORS headers for non-allowed origins; browser
        # will reject preflight, which is the desired outcome.
        return PlainTextResponse('')
    return PlainTextResponse(
        '',
        headers={
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Credentials': 'true',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Max-Age': '86400',
            'Vary': 'Origin',
        }
    )


__all__ = [
    "_dispatch_through_tool",
    "_build_route_principal",
    "_result_text",
    "handle_options",
    "ALLOWED_ORIGINS",
]
