# Agent-MCP/mcp_template/mcp_server_src/app/main_app.py
import asyncio
import contextvars
import json
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI
from starlette.routing import Mount
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

# MCP Server specific imports
from mcp.server.lowlevel import Server as MCPLowLevelServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
import mcp.types as mcp_types

# Project-specific imports
from ..core.config import logger
from ..core.auth import get_agent_id, query_agent_status
from ..core import session_registry
from .routers import register_routers
from .server_lifecycle import application_startup, application_shutdown
from ..tools.registry import (
    list_available_tools,
    dispatch_tool_call,
    request_auth_token,
    request_principal,
)


# --- Migration response for the retired SSE transport endpoints ----
# PR: Streamable HTTP transport per MCP spec rev 2025-03-26.
# The old `/sse` + `/messages/?session_id=...` pair maintained per-
# session state (`mcp.server.sse.SseServerTransport._read_stream_writers`,
# a dict keyed by UUID) that was lost on every backend restart. Clients
# that opened a session before the restart would then POST to a
# `session_id` the backend had no record of and get a bare 404 with no
# actionable next step. The atomic flip to `/mcp` removes the dict
# entirely (stateless mode); the old endpoints return 410 Gone with
# this body so any client/router still hitting them gets pointed at
# the migration path.
_MIGRATION_BODY = json.dumps(
    {
        "error": "endpoint_removed",
        "migrated_to": "/mcp",
        "spec_revision": "2025-03-26",
        "hint": (
            "Use POST /mcp with Authorization: Bearer <token>. "
            "Sessions are no longer required."
        ),
    }
).encode("utf-8")


# Phase 1c — request-scoped alias telemetry.
#
# Set by ``AuthHeaderMiddleware`` when the upstream router (Phase 1b)
# proxied a request from an alias URL and forwarded the
# ``X-Agent-MCP-Alias: <name>,<expires_at>`` header. Read by the
# overridden ``create_initialization_options`` so the MCP initialize
# response's ``instructions`` field can append a deprecation warning,
# and by the GET /mcp opener so the new ``mcp_sessions.alias_used``
# column records which alias routed the stream.
#
# ContextVar (rather than ``request.scope``) is required for the
# initialize-response path because the SDK calls
# ``create_initialization_options`` from a server-task spawned by
# ``StreamableHTTPSessionManager`` — that task inherits the request's
# Context (so the ContextVar carries over) but has no direct handle on
# the originating Request object.
request_alias_info: "contextvars.ContextVar[Optional[tuple[str, str]]]" = (
    contextvars.ContextVar("request_alias_info", default=None)
)


class _GoneApp:
    """ASGI app that always responds 410 Gone with the migration body.

    Used for the retired `/sse` and `/messages` mounts. The body is
    JSON so clients/routers can parse it without scraping; the spec
    revision identifies the version of the Streamable HTTP transport
    the backend has migrated to.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 410,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_MIGRATION_BODY)).encode()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": _MIGRATION_BODY,
                "more_body": False,
            }
        )


def _parse_alias_header(value: Optional[str]) -> Optional[tuple[str, str]]:
    """Parse the `X-Agent-MCP-Alias: <name>,<expires_at>` header value.

    Returns `(alias_name, expires_at_iso)` on success, `None` if the
    header is absent or malformed. The router (Phase 1b) is the only
    legitimate producer of this header; if a client sets it directly
    the worst case is a cosmetic warning in the initialize response,
    so we don't try to authenticate the header itself.

    Both halves are required — a value missing the comma means the
    router shape changed and we should not silently inject half-data
    into the response. Returning None there leaves the wire response
    unchanged (the no-alias path).
    """
    if not value:
        return None
    parts = value.split(",", 1)
    if len(parts) != 2:
        return None
    alias_name = parts[0].strip()
    expires_at = parts[1].strip()
    if not alias_name or not expires_at:
        return None
    return alias_name, expires_at


def _build_unauthorized_response(token: str):
    """Craft the 401 response body for a /mcp request that failed
    the in-memory bearer check.

    Two outcomes:

    1. The bearer is the PK of a real row in the `agents` table whose
       status is ``'terminated'`` — the agent was terminated and its
       token, though structurally valid, no longer authenticates. We
       return an `agent_terminated` envelope naming the agent_id, the
       `terminated_at` timestamp, and the `restore_agent` tool path
       so the client (Claude Code, a CLI, an integration test) can
       tell the operator exactly what to do.

       This case used to surface as the generic 401 below — the user
       saw "Server rejected the configured Authorization header. Check
       that the token is valid." even though the token *was* valid;
       only the agent was gone. Specific error → specific fix.

    2. Anything else (no token, wrong bearer, unknown agent_id) →
       `invalid_bearer` envelope. Migrated from the prior plain-text
       body to JSON so consumers can rely on a single response shape
       across both failure modes.

    DB-failure note: `query_agent_status` returns None on any DB
    error (the engine layer logs it). The middleware falls through
    to the `invalid_bearer` branch — never silently misattribute a
    transient DB blip to "your agent was terminated".
    """
    from starlette.responses import JSONResponse

    info = query_agent_status(token) if token else None
    if info is not None and info.get("status") == "terminated":
        agent_id = info.get("agent_id")
        terminated_at = info.get("terminated_at")
        message = (
            f"Agent {agent_id!r} was terminated on {terminated_at}. "
            f"Use the restore_agent tool to revive it, or rotate to a "
            f"different agent token."
        )
        return JSONResponse(
            {
                "error": "agent_terminated",
                "agent_id": agent_id,
                "terminated_at": terminated_at,
                "message": message,
            },
            status_code=401,
        )

    return JSONResponse(
        {
            "error": "invalid_bearer",
            "message": (
                # retire-system-token Wave 1: the system_token god-key
                # is no longer accepted as a bearer on any backend
                # route. The valid surfaces are now (a) a per-agent
                # token from the agents table, or (b) the router's
                # signed forwarding header.
                "Bearer token does not match any active agent. Send "
                "Authorization: Bearer <per-agent-token> on POST /mcp, "
                "or route the request through the router so it can "
                "attach the signed forwarding header."
            ),
        },
        status_code=401,
    )


def _build_principal_from_request(
    *,
    request,
    bearer_token: str,
    forwarding_operator: Optional[str],
):
    """Construct the per-request :class:`Principal` for the per-project backend.

    Built once at the outermost seam that knows the forwarding-header
    + bearer state, then stashed on ``request.state.principal`` AND
    on the :data:`tools.registry.request_principal` ContextVar (so
    the MCP wire handler, which has no Request handle, can thread it
    into :func:`dispatch_tool_call`).

    Resolution order:

    * If the signed forwarding header verified, the caller is an
      operator who arrived via the router. Build a
      ``forwarding_header`` Principal naming that operator. We do
      NOT resolve the operator's project role here — the per-project
      backend has no router.db handle, so the project-role gate
      remains the router middleware's job. ``project_role`` is None;
      the in-process ``has_role`` check uses ``kind`` for the
      operator-tier admit.
    * If a per-agent bearer authenticated, build an ``agent_bearer``
      Principal sourcing ``agent_id`` + ``agent_role`` from the
      agents table (via the in-memory cache). ``can_wake_loop``
      mirrors the wake-loop instructions' eligibility check (consumed
      by ``_wake_loop_contributor``).
    * If neither admitted (auth-less route or an unauth-required
      path), return None.

    Failures are defensive: any exception returns None so a buggy
    Principal-build path can never block a request the legacy path
    would have admitted.
    """
    try:
        from ..core.capabilities import resolve_capabilities
        from ..core.principal import Principal

        if forwarding_operator:
            # Wave 9 PR 0: capabilities resolved at the seam; threaded
            # into Principal once. The per-project backend has no
            # router.db handle so the group-cap overlay returns empty
            # here — the router middleware (which DOES have router.db)
            # has already resolved + admitted the operator via the
            # cookie path, so the forwarding-header Principal here is
            # purely the in-process restatement of that admit.
            caps = resolve_capabilities(
                user_id=forwarding_operator,
                agent_id=None,
                sysadmin=False,
                agent_role=None,
                project_role=None,
                kind="forwarding_header",
            )
            return Principal(
                kind="forwarding_header",
                user_id=forwarding_operator,
                agent_id=None,
                sysadmin=False,
                project_name=None,
                project_role=None,
                agent_role=None,
                can_wake_loop=False,
                source_token=None,
                capabilities=caps,
            )
        if bearer_token:
            agent_id = get_agent_id(bearer_token)
            if agent_id:
                from ..core import globals as _g
                row = _g.active_agents.get(bearer_token) or {}
                agent_role = row.get("agent_role")
                normalized_role = (
                    agent_role
                    if agent_role in ("worker", "manager")
                    else None
                )
                # Wake-loop eligibility — admin agents coordinate
                # and don't run the worker wake loop; non-admin
                # agents qualify when the global toggle is on AND
                # their per-agent flag is on (default True). The
                # per-agent flag is sourced from the DB rather than
                # the in-memory cache so an operator who flipped the
                # flag via REST in the current session sees the
                # change reflected on the next request.
                can_wake_loop = False
                if agent_id != "admin":
                    try:
                        from ..tools import access as _access
                        from ..db.connection import get_db_connection
                        global_on = _access._get_config_bool(
                            "config_auto_event_loop_global", default=True,
                        )
                        if global_on:
                            conn = get_db_connection()
                            try:
                                cursor = conn.cursor()
                                cursor.execute(
                                    "SELECT auto_event_loop FROM agents "
                                    "WHERE agent_id = ?",
                                    (agent_id,),
                                )
                                db_row = cursor.fetchone()
                            finally:
                                conn.close()
                            if db_row is not None and bool(db_row["auto_event_loop"]):
                                can_wake_loop = True
                    except Exception:  # pragma: no cover - defensive
                        can_wake_loop = False
                # Wave 9 PR 0: capabilities resolved at the seam;
                # threaded into Principal once. Agent-bearer caps come
                # from AGENT_ROLE_BUNDLES[agent_role] alone — group
                # caps don't apply (they're operator-shaped).
                caps = resolve_capabilities(
                    user_id=None,
                    agent_id=agent_id,
                    sysadmin=False,
                    agent_role=normalized_role,
                    project_role=None,
                    kind="agent_bearer",
                )
                return Principal(
                    kind="agent_bearer",
                    user_id=None,
                    agent_id=agent_id,
                    sysadmin=False,
                    project_name=None,
                    project_role=None,
                    agent_role=normalized_role,
                    can_wake_loop=can_wake_loop,
                    source_token=bearer_token,
                    capabilities=caps,
                )
        return None
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "Principal construction failed in AuthHeaderMiddleware",
        )
        return None


class AuthHeaderMiddleware(BaseHTTPMiddleware):
    """Capture Authorization: Bearer into request_auth_token + gate /mcp.

    Four responsibilities:

    1. Bind any incoming ``Authorization: Bearer <tok>`` value to the
       ``request_auth_token`` ContextVar so tool dispatch can fall
       back to it when JSON-RPC arguments don't carry a ``token``
       field. The Streamable HTTP transport reads from the same
       ContextVar.

    2. Verify the signed forwarding header
       (``X-Agent-MCP-Forwarded-Operator``) if present and the
       per-project HMAC key has been loaded into
       ``g.forwarding_hmac_key``. On success the resolved operator_id
       is stamped onto ``g.current_operator`` for downstream handlers
       + audit logs. On a present-but-invalid header (wrong HMAC,
       expired, malformed) the request is rejected with 401 — we
       never silently fall through to the bearer-token path.

    3. Gate ``/mcp`` at the HTTP layer. POST/GET/DELETE on ``/mcp``
       must carry either (a) a per-agent bearer that resolves to an
       active agent row, OR (b) a verified forwarding header.

       Per-tool role checks (operator vs manager vs worker) still
       happen at the dispatcher / decorator layer via
       :func:`agent_mcp.core.authorize.requires_role`; this middleware
       only enforces "is this *any* valid caller identity?".

    4. Stamp the typed Principal on the request AND on the
       :data:`tools.registry.request_principal` ContextVar so the MCP
       wire handler (which has no Request handle) can thread it into
       :func:`dispatch_tool_call`.

    5. Phase 1c: parse ``X-Agent-MCP-Alias`` if present and stash the
       ``(alias_name, expires_at)`` tuple on ``request.scope`` plus
       the ``request_alias_info`` ContextVar.
    """

    async def dispatch(self, request, call_next):
        # Local imports — the middleware module loads at app
        # construction time; deferring these keeps the import graph
        # cheap for unit tests that import ``main_app`` for non-HTTP
        # introspection (route shape, etc.).
        from . import forwarding_header as _fh
        from ..core import globals as _g

        # Reset request-scoped state. ``current_operator`` is a
        # process-wide global by storage shape; we treat it as
        # request-scoped by clearing on entry so a previous request's
        # operator_id never leaks into a request that authenticates
        # via a per-agent bearer.
        _g.current_operator = None

        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            request_auth_token.set(token)

        # Phase 1c — alias telemetry. Parse here even for non-/mcp
        # requests because dashboard/REST aliasing may come later and
        # this keeps the contract uniform: the header always lands on
        # the scope if present, and consumers downstream check scope
        # without re-parsing.
        alias_info = _parse_alias_header(
            request.headers.get("X-Agent-MCP-Alias")
        )
        if alias_info is not None:
            request.scope["agent_mcp_alias"] = alias_info
            request_alias_info.set(alias_info)

        # Signed forwarding header path. Dormant when the per-project
        # HMAC key isn't loaded yet (Wave 2/3 wire the launcher write
        # side); active when it is.
        forwarding_raw = request.headers.get(_fh.HEADER_NAME)
        forwarding_operator: Optional[str] = None
        if forwarding_raw is not None:
            if _g.forwarding_hmac_key:
                forwarding_operator = _fh.verify(
                    forwarding_raw, _g.forwarding_hmac_key
                )
                if forwarding_operator is None:
                    # Present-but-invalid header: reject hard. Falling
                    # through to the bearer path would let a tampered
                    # header silently downgrade auth to "whatever bearer
                    # was attached", which is the wrong defaults-secure
                    # behaviour.
                    return _build_unauthorized_response(token)
                _g.current_operator = forwarding_operator
            else:
                # Key not loaded yet — dormant fallback. The header
                # is ignored (no operator identity established) but
                # we do NOT reject, so an agent bearer + a
                # speculatively-set header still authenticates via
                # the bearer path.
                forwarding_operator = None

        path = request.url.path
        # Gate the MCP transport endpoint. We match exact `/mcp` and
        # `/mcp/...` so a future sub-path doesn't accidentally bypass
        # auth. `/api/*` and the dashboard routes keep their own
        # per-route token handling.
        if path == "/mcp" or path.startswith("/mcp/"):
            # Cache-only check (no DB roundtrip) so a terminated
            # agent's token (which the repo would happily resolve
            # against the DB row + re-populate the cache as a
            # side effect) cleanly fails auth — the cache holds only
            # non-terminated rows.
            authenticated = bool(forwarding_operator) or (
                bool(token) and token in _g.active_agents
            )
            if not authenticated:
                return _build_unauthorized_response(token)

        # Build a Principal once at the seam that admitted the
        # request. Stashed on request.state for FastAPI deps AND on
        # request_principal so the MCP wire handler reads the same
        # identity (it has no Request handle to read request.state).
        principal = _build_principal_from_request(
            request=request,
            bearer_token=token,
            forwarding_operator=forwarding_operator,
        )
        if principal is not None:
            request.state.principal = principal
            request_principal.set(principal)

        return await call_next(request)


# --- MCP Server Setup ----------------------------------------------
mcp_app_instance = MCPLowLevelServer("mcp-server")


# Phase 1c — alias deprecation warning block, appended to the MCP
# `initialize` response's top-level `instructions` field when the
# request arrived via an alias URL. Per spec rev 2025-03-26 the field
# is `InitializeResult.instructions` (sibling of `serverInfo`, not
# nested inside it) — clients display it as authoritative guidance.
_ALIAS_WARNING_TEMPLATE = (
    "\n\n"
    "ALIAS DEPRECATION WARNING\n"
    "This server was reached via the alias '{alias_name}', which is "
    "scheduled to expire on {expires_at}. Once the alias expires, "
    "this URL will stop working.\n"
    "Ask your operator to update your MCP client configuration to use "
    "the canonical project name before that date."
)


def _build_alias_warning(alias_name: str, expires_at: str) -> str:
    """Format the deprecation warning block for one alias/expiry pair.

    Extracted so tests + future callers (e.g. a CLI `agent-mcp router
    list-aliases` command that wants the same wording) can reuse it
    without re-implementing the template.
    """
    return _ALIAS_WARNING_TEMPLATE.format(
        alias_name=alias_name, expires_at=expires_at
    )


def _patched_create_initialization_options(self, *args, **kwargs):
    """Wrap ``Server.create_initialization_options`` so registered
    ``InstructionsContributor`` callables can append text onto the
    ``instructions`` field of the MCP ``initialize`` response.

    Called once per request in stateless mode (the SDK's
    ``StreamableHTTPSessionManager._handle_stateless_request`` spawns
    a fresh server task per request, and that task calls this method
    immediately). We assemble an ``InitContext`` from the request-
    scoped ContextVars (``request_alias_info``, ``request_auth_token``)
    — set by ``AuthHeaderMiddleware`` on the incoming request — and
    hand it to ``render_all`` which walks the registry.

    Pre-PR-W1b this method inlined both the alias-warning and wake-
    loop logic; the registry in ``instructions_contributors.py`` now
    owns the chain, so adding a third contributor is one
    ``register(...)`` call instead of a reach-in here. The wire
    output is byte-identical: same contributors, same gating, same
    text, same ordering.
    """
    from .instructions_contributors import InitContext, render_all

    base = _ORIG_CREATE_INIT_OPTIONS(self, *args, **kwargs)
    try:
        bearer = request_auth_token.get()
    except LookupError:
        bearer = None
    principal = request_principal.get()
    ctx = InitContext(
        bearer=bearer or None,
        alias_info=request_alias_info.get(),
        principal=principal,
    )
    extra = render_all(ctx)
    if not extra:
        return base
    base.instructions = (base.instructions or "") + extra
    return base


# Capture the original *before* monkey-patching so the wrapper has a
# stable handle. Class-level method override (rather than per-instance
# attribute) keeps the patch visible to any future Server subclass that
# we might construct down the line.
_ORIG_CREATE_INIT_OPTIONS = MCPLowLevelServer.create_initialization_options
MCPLowLevelServer.create_initialization_options = (  # type: ignore[assignment]
    _patched_create_initialization_options
)


@mcp_app_instance.list_tools()
async def mcp_list_tools_handler() -> List[mcp_types.Tool]:
    """MCP endpoint to list available tools."""
    principal = request_principal.get()
    if principal is None:
        # Fallback for in-process / test callers that haven't stamped
        # request_principal but did stamp request_auth_token: build an
        # agent_bearer Principal locally so the visibility filter
        # resolves the same role label production would.
        try:
            bearer = request_auth_token.get()
        except LookupError:
            bearer = None
        if bearer:
            principal = _build_principal_from_request(
                request=None,
                bearer_token=bearer,
                forwarding_operator=None,
            )
    return await list_available_tools(principal=principal)


def _principal_role() -> str:
    """Resolve the calling Principal's role for visibility filtering.

    Returns ``"admin"`` for operator-tier callers, ``"worker"`` for
    any agent bearer, ``"anonymous"`` when no Principal is in flight.

    Falls back to a synthesized ``agent_bearer`` Principal built from
    :data:`request_auth_token` for in-process callers (tests, scripts)
    that haven't stamped :data:`request_principal` directly.
    """
    principal = request_principal.get()
    if principal is None:
        try:
            bearer = request_auth_token.get()
        except LookupError:
            bearer = None
        if bearer:
            from ..core import globals as _g
            agent_id = get_agent_id(bearer)
            if agent_id == "admin":
                return "admin"
            if agent_id:
                return "worker"
        return "anonymous"
    if principal.has_role("admin"):
        return "admin"
    if principal.kind == "agent_bearer":
        return "worker"
    return "anonymous"


@mcp_app_instance.list_prompts()
async def mcp_list_prompts_handler() -> List[mcp_types.Prompt]:
    """Return the Prompt Book catalogue (plan Phase 6, filtered by
    role per Candidate B / G).

    Sourced from `agent_mcp/prompts/catalog.json` via the shared
    `prompt_registry`. Entries with `"visibility": "admin"` are
    hidden from worker + anonymous callers; default is `"any"` so
    the on-the-wire behavior is unchanged for the current catalog.
    """
    from ..prompts import prompt_registry

    role = _principal_role()
    prompts: List[mcp_types.Prompt] = []
    for entry in prompt_registry.list_visible(role):
        args = []
        for v in entry.meta.variables:
            args.append(
                mcp_types.PromptArgument(
                    name=v["name"],
                    description=v.get("description", ""),
                    required=bool(v.get("required", False)),
                )
            )
        prompts.append(
            mcp_types.Prompt(
                name=entry.name,
                title=entry.meta.title,
                description=entry.meta.description,
                arguments=args,
            )
        )
    return prompts


@mcp_app_instance.get_prompt()
async def mcp_get_prompt_handler(
    name: str, arguments: Optional[dict] = None
) -> mcp_types.GetPromptResult:
    """Render a Prompt Book entry with the supplied arguments
    substituted into its `{{VARIABLE}}` placeholders.

    Returns the rendered text as a single user-role message.
    Missing optional variables substitute as empty (no
    `{{VAR}}` leaks through). Admin-only prompts (per the catalog's
    `"visibility": "admin"` field) raise PermissionError for
    non-admin callers — defense in depth on top of `prompts/list`
    filtering, since a worker could otherwise guess the id.
    """
    from ..prompts import prompt_registry

    role = _principal_role()
    entry = prompt_registry.get(name)
    if entry is None:
        raise ValueError(f"Unknown prompt: {name}")
    # `render()` enforces visibility; PermissionError propagates as a
    # JSON-RPC error via the framework wrapper.
    rendered = prompt_registry.render(name, arguments or {}, role=role)
    return mcp_types.GetPromptResult(
        description=entry.meta.description,
        messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(type="text", text=rendered),
            )
        ],
    )


@mcp_app_instance.list_resources()
async def mcp_list_resources_handler() -> List[mcp_types.Resource]:
    """Return the per-caller resource URIs for every registered
    resource (plan Phase 3, refactored to Registry[T] in Candidate B).

    Each entry in `resource_registry` is scoped to the calling
    bearer's agent_id (admin sees their own admin-scoped pair;
    workers see their own). Cross-agent reads are rejected at
    `resources/read` time. Unauthenticated callers see an empty
    list — same UX choice as `tools/list` for anonymous role.
    """
    from ..core.auth import get_agent_id
    from ..resources import resource_registry
    from pydantic_core import Url

    token = request_auth_token.get()
    agent_id = get_agent_id(token) if token else None
    if not agent_id:
        return []

    role = "admin" if agent_id == "admin" else "worker"
    resources: List[mcp_types.Resource] = []
    for entry in resource_registry.list_visible(role):
        resources.append(
            mcp_types.Resource(
                uri=Url(f"{entry.meta.uri_prefix}{agent_id}"),
                name=f"{entry.name}/{agent_id}",
                description=entry.meta.description,
                mimeType=entry.meta.mime_type,
            )
        )
    return resources


@mcp_app_instance.read_resource()
async def mcp_read_resource_handler(uri):
    """Read a resource by URI (plan Phase 3, refactored to
    Registry[T] in Candidate B).

    Dispatch walks `resource_registry` for the URI prefix that
    matches — adding a new resource means registering it, no
    if/elif chain to update here.

    Cross-agent reads are rejected; admin can read any agent's
    resources (operational visibility).
    """
    from ..resources import resource_registry
    from mcp.server.lowlevel.helper_types import ReadResourceContents

    uri_str = str(uri)
    token = request_auth_token.get()
    # `read()` raises ValueError on auth mismatch / unknown URI —
    # the framework surfaces the message verbatim as a JSON-RPC error.
    text = resource_registry.read(uri_str, token)

    entry = resource_registry.find_by_uri(uri_str)
    mime = entry.meta.mime_type if entry else "application/json"
    return [ReadResourceContents(content=text, mime_type=mime)]


@mcp_app_instance.call_tool(validate_input=False)
async def mcp_call_tool_handler(name: str, arguments: dict) -> List[mcp_types.TextContent]:
    """MCP endpoint to call a specific tool.

    ``validate_input=False`` disables the framework's automatic
    ``jsonschema.validate(arguments, tool.inputSchema)`` step
    (mcp/server/lowlevel/server.py:497). We re-run validation inside
    :func:`dispatch_tool_call` *after* cleaning arguments for the
    real-world shapes LLM clients produce (``token: null``, leaked
    ``_meta``, integer-as-string). See ``_clean_arguments_for_schema``
    in ``tools/registry.py`` and the tolerance suite in
    ``tests/test_call_tool_argument_tolerance.py``.

    Wave 6 PR 6: the Principal is read from the
    :data:`tools.registry.request_principal` ContextVar (stamped by
    :class:`AuthHeaderMiddleware` on every authenticated request) and
    threaded explicitly into the dispatcher. The legacy ContextVar
    bridge in ``dispatch_tool_call`` is gone — ``principal`` is now
    a required kwarg.
    """
    from ..core.tool_result import render_as_text_content

    principal = request_principal.get()
    if principal is None:
        # Fallback for in-process / test callers that haven't stamped
        # request_principal but did stamp request_auth_token: build a
        # Principal from the bearer so the dispatcher / per-tool
        # decorator sees the same identity production would.
        try:
            bearer = request_auth_token.get()
        except LookupError:
            bearer = None
        if bearer:
            principal = _build_principal_from_request(
                request=None,
                bearer_token=bearer,
                forwarding_operator=None,
            )
    result = await dispatch_tool_call(name, arguments, principal=principal)
    return render_as_text_content(result)


# --- Streamable HTTP transport (spec rev 2025-03-26) --------------
# Replaces the legacy SseServerTransport (which paired GET /sse with
# POST /messages/?session_id=...). The old transport's per-session
# `_read_stream_writers` dict died on every backend restart, leaving
# pre-restart clients POSTing to dead session_ids. StreamableHTTP in
# *stateless* mode creates a fresh transport per request — there is no
# session bookkeeping to lose, so a backend restart is invisible to
# clients beyond the in-flight request itself.
#
# Spec endpoint shape:
#   POST /mcp     — request/response (inline JSON or SSE body)
#   GET  /mcp     — long-lived SSE for server-initiated notifications
#   DELETE /mcp   — session termination (405 in stateless mode)
#
# Module-level alias updated by `create_app` so tests / introspection
# can reach the live manager. Construction itself happens inside
# `create_app` because `StreamableHTTPSessionManager.run()` is
# single-shot (one call per instance), and the test suite builds a
# fresh app per test.
session_manager: Optional[StreamableHTTPSessionManager] = None


class _McpAsgiApp:
    """ASGI app that delegates to a StreamableHTTP session manager.

    Bound to its manager at construction time (rather than dereferring
    the module-global `session_manager` on every request) so tests
    that build multiple `create_app` instances in the same process
    don't accidentally hit each other's stale managers.

    GET /mcp is handled IN-HOUSE rather than passed through to the
    SDK's manager because in stateless mode the SDK creates a fresh
    transport per request — its GET stream is local to that one
    transport, dies with the request, and is therefore useless for
    cross-request notification fan-out. Our handler:

      1. Registers an ``mcp_sessions`` row via ``session_registry``
         (bearer hashed, agent_id derived from the bearer-resolved
         active_agents entry).
      2. Attaches an ``asyncio.Queue`` to the session_id.
      3. Streams SSE to the wire, draining the queue → one
         ``data: <json-rpc envelope>`` frame per payload.
      4. On disconnect / error: detaches the queue + unregisters the
         row in a try/finally so a crash mid-pump still cleans up.

    POST and DELETE still pass through to the manager: POST handles
    the JSON-RPC request/response shape (inline JSON or SSE body
    when a tool emits progress); DELETE returns 405 in stateless mode.
    """

    # Heartbeat cadence for the GET stream. Sent as SSE comment lines
    # (`: ping`) which clients ignore. Two purposes:
    #
    # 1. Detect dead TCP peers: a write to a closed socket will raise
    #    inside `send`, which is how the cleanup `finally` learns the
    #    client is gone (ASGI's `http.disconnect` event arrives via
    #    `receive` but only when nothing else is awaiting receive,
    #    which our pump loop isn't).
    # 2. Keep intermediaries (corporate proxies, load balancers) from
    #    reaping the long-lived connection as idle.
    _HEARTBEAT_INTERVAL_SECONDS = 15.0

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") == "GET":
            await self._handle_get(scope, receive, send)
            return
        await self._manager.handle_request(scope, receive, send)

    async def _handle_get(self, scope, receive, send) -> None:
        """Open an SSE stream + drain `session_registry` queue at it.

        Pre-condition: ``AuthHeaderMiddleware`` has already verified
        the bearer (any /mcp request without a valid bearer is
        rejected with 401 before reaching here), so we trust the
        Authorization header value in `scope.headers` here for
        identity-resolution purposes (`get_agent_id`).
        """
        bearer = _bearer_from_scope(scope)
        agent_id = get_agent_id(bearer) if bearer else None
        if not agent_id:
            # Defence in depth — middleware should have already
            # rejected this. Return 500 rather than 401 because a
            # mismatch here is a backend bug (the middleware's gate
            # passed but agent resolution failed), not a client
            # auth problem we want clients to retry.
            await _send_simple_response(
                send, 500, b'{"error":"session_registry_no_agent"}'
            )
            return

        # Phase 1c — surface the alias name (if any) on the persisted
        # session row. The middleware stashes the parsed tuple on
        # `scope["agent_mcp_alias"]`; we pull `alias_name` here. The
        # `expires_at` half is not stored on the session row — it's
        # purely a router-side fact used to build the instructions
        # warning.
        alias_tuple = scope.get("agent_mcp_alias")
        alias_name = alias_tuple[0] if alias_tuple else None
        session_id = session_registry.register_session(
            agent_id=agent_id,
            bearer_token=bearer,
            alias_used=alias_name,
        )
        # Cache the Principal alongside the runtime queue so the
        # per-tool-call dispatcher (which runs in a task spawned
        # past the middleware return) can read identity without
        # re-deriving. The Principal is built fresh here against the
        # same bearer that opened the stream — it lives until the
        # session dies in the cleanup finally below.
        try:
            principal = _build_principal_from_request(
                request=None,
                bearer_token=bearer,
                forwarding_operator=None,
            )
            if principal is not None:
                session_registry.attach_principal(session_id, principal)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "session_registry: failed to cache principal for session=%s",
                session_id,
            )
        # The queue size is intentionally bounded — if a client's
        # consumption falls behind by more than this many notifications
        # we drop oldest and log. 256 fits a worker that's been
        # disconnected for ~minutes at typical notification rates.
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        session_registry.attach_runtime_queue(session_id, queue)
        logger.info(
            "session_registry: opened GET /mcp stream session=%s agent=%s",
            session_id, agent_id,
        )

        try:
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-cache, no-transform"),
                    (b"connection", b"keep-alive"),
                ],
            })
            # Initial comment frame so headers flush immediately — let
            # the client know the connection is live without having to
            # wait for the first real notification.
            await send({
                "type": "http.response.body",
                "body": b": connected\n\n",
                "more_body": True,
            })

            # Concurrent watchers: drain the queue while also watching
            # the client-disconnect signal. Whichever finishes first
            # ends the request; the other is cancelled in the finally.
            disconnect_task = asyncio.create_task(
                _await_disconnect(receive),
                name=f"mcp-get-disconnect-{session_id}",
            )
            pump_task = asyncio.create_task(
                self._pump(session_id, queue, send),
                name=f"mcp-get-pump-{session_id}",
            )
            try:
                done, pending = await asyncio.wait(
                    {disconnect_task, pump_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # Surface pump errors (disconnect is expected to
                # finish first under normal usage).
                for t in done:
                    if t is pump_task and not t.cancelled():
                        exc = t.exception()
                        if exc is not None:
                            raise exc
            finally:
                disconnect_task.cancel()
                pump_task.cancel()

            try:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            except Exception:  # pragma: no cover - peer already gone
                pass
        finally:
            session_registry.detach_runtime_queue(session_id)
            session_registry.unregister_session(session_id)
            logger.info(
                "session_registry: closed GET /mcp stream session=%s agent=%s",
                session_id, agent_id,
            )

    async def _pump(self, session_id: str, queue: asyncio.Queue, send) -> None:
        """Drain `queue` onto the SSE wire forever (until cancelled).

        One SSE `data:` frame per queue payload. Heartbeat comments
        are emitted whenever the queue stays empty past
        `_HEARTBEAT_INTERVAL_SECONDS` — they double as the dead-peer
        detector (a write to a closed socket raises here, which
        bubbles up and ends the request via the surrounding `wait`).

        We also call `session_registry.touch_session` on every
        successful payload + heartbeat so the periodic pruner doesn't
        evict still-live sessions.
        """
        while True:
            try:
                payload = await asyncio.wait_for(
                    queue.get(), timeout=self._HEARTBEAT_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                await send({
                    "type": "http.response.body",
                    "body": b": heartbeat\n\n",
                    "more_body": True,
                })
                try:
                    session_registry.touch_session(session_id)
                except Exception:  # pragma: no cover - defensive
                    pass
                continue

            data = json.dumps(payload).encode("utf-8")
            await send({
                "type": "http.response.body",
                "body": b"data: " + data + b"\n\n",
                "more_body": True,
            })
            try:
                session_registry.touch_session(session_id)
            except Exception:  # pragma: no cover - defensive
                pass


def _bearer_from_scope(scope) -> str:
    """Extract the bearer value from an ASGI HTTP scope's headers.

    Returns "" if absent or malformed. We re-parse here (rather than
    reading from `request_auth_token`) because the ContextVar is set
    by ``AuthHeaderMiddleware`` on the incoming request task — the
    pump task that lives past the middleware return runs in a
    different context where the var may have been reset.
    """
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            try:
                decoded = value.decode("ascii")
            except UnicodeDecodeError:
                return ""
            if decoded.lower().startswith("bearer "):
                return decoded[7:].strip()
            return ""
    return ""


async def _await_disconnect(receive) -> None:
    """Block until the ASGI `http.disconnect` event arrives.

    ASGI delivers `http.disconnect` when the client (or upstream proxy)
    closes the TCP connection. Returning from this coroutine signals
    to `_handle_get` that it should tear down the SSE stream.
    """
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return


async def _send_simple_response(send, status: int, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


# --- FastAPI Application Creation ----------------------------------
def create_app(
    project_dir: str,
) -> FastAPI:
    """Build and configure the main FastAPI application.

    Phase 1 PR A (prancy-napping-pie): migrated from Starlette to
    FastAPI. FastAPI is a Starlette subclass so the wire contract is
    unchanged — the auth middleware, lifespan, CORS, and mounted
    Starlette routes (``/mcp``, ``/sse``, ``/messages``) all attach to
    the FastAPI instance via the same Starlette APIs. Subsequent PRs
    (B/C/D) will convert the route layer's ad-hoc ``Request``-based
    handlers to typed FastAPI signatures and replace the
    BaseHTTPMiddleware-style auth with a ``Depends(...)`` dep.

    Lifespan: FastAPI accepts the same ``@asynccontextmanager`` shape
    via the ``lifespan=`` kwarg. We chain the app's own
    startup/shutdown with the StreamableHTTP session manager's
    ``.run()`` context — the SDK requires ``.run()`` to wrap request
    handling, otherwise the manager's task group is not initialised
    and ``handle_request`` raises RuntimeError.
    """

    # Build a fresh StreamableHTTP session manager per app instance.
    # `StreamableHTTPSessionManager.run()` is single-shot — calling
    # it twice on the same instance raises. The test suite builds a
    # new app per test, so the manager has to be per-app too.
    global session_manager
    manager = StreamableHTTPSessionManager(
        app=mcp_app_instance,
        event_store=None,
        json_response=False,
        stateless=True,
    )
    session_manager = manager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await application_startup(
            project_dir_path_str=project_dir,
        )
        logger.info(
            "FastAPI app startup complete. Background tasks should be started by the server runner."
        )
        # `manager.run()` must wrap any request handling — it creates
        # the task group that spawns per-request server tasks.
        async with manager.run():
            try:
                yield
            finally:
                await application_shutdown()
                logger.info("FastAPI app shutdown complete.")

    middleware_stack = [
        Middleware(AuthHeaderMiddleware),
        Middleware(
            CORSMiddleware,
            allow_origins=[
                'http://localhost:3847',
                'http://127.0.0.1:3847',
                'http://localhost:3000',
                'http://localhost:3001',
                '*',
            ],
            allow_credentials=True,
            allow_methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'HEAD', 'PATCH'],
            allow_headers=['*'],
            expose_headers=['*'],
            max_age=3600,
        ),
    ]

    # Mount the bare-ASGI routes (``/mcp``, ``/sse``, ``/messages``)
    # before constructing the FastAPI app so they participate in the
    # initial ``routes=`` list. FastAPI doesn't intercept Mounts it
    # doesn't define, so these stay pass-through ASGI.
    mounted_routes = [
        # New Streamable HTTP transport at /mcp (spec rev 2025-03-26).
        Mount('/mcp', app=_McpAsgiApp(manager), name="mcp_transport"),
        # Legacy endpoints — 410 Gone with the migration JSON body.
        # Kept mounted (rather than deleted outright) so any
        # client/router still pointed at the old shape gets a
        # structured, parseable hint rather than a bare 404. Remove
        # these mounts in a later major version once telemetry shows
        # no traffic.
        Mount('/sse', app=_GoneApp(), name="legacy_sse_gone"),
        Mount('/messages', app=_GoneApp(), name="legacy_messages_gone"),
    ]

    app = FastAPI(
        routes=mounted_routes,
        lifespan=lifespan,
        middleware=middleware_stack,
        debug=os.environ.get("MCP_DEBUG", "false").lower() == "true",
    )

    # Dashboard REST handlers live in per-resource ``APIRouter``
    # modules under :mod:`agent_mcp.app.routers`. See
    # :func:`agent_mcp.app.routers.register_routers` for the mount
    # order (settings ships last because it owns the ``/api`` OPTIONS
    # catch-all).
    register_routers(app)

    logger.info("FastAPI application instance created with routes and lifecycle events.")
    return app

# The actual running of the app (e.g., with uvicorn) will be handled by cli.py
