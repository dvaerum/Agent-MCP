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
from ._dispatch_helpers import ALLOWED_ORIGINS
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
    forwarding_role: Optional[str] = None,
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
      ``forwarding_header`` Principal naming that operator. The
      per-project backend has no router.db handle, so group-cap
      overlays don't apply here — but the operator carries
      ``project_role=forwarding_role`` (the REAL role the router
      resolved from ``project_membership`` and signed into the
      header) so :func:`resolve_capabilities` back-fills the matching
      bundle: viewer caps for a viewer, operator caps for an operator.
      SEC-1 (2026-07): this used to hard-code
      ``project_role="operator"`` for every verified header, which
      handed a viewer-tier operator the full operator bundle
      (agents.register / terminate, system.config.write, …) over the
      MCP wire even though the REST ``/api/`` surface correctly 403'd
      them. The role now rides the HMAC-signed header, so it can't be
      tampered in flight and the wire path matches the REST path's
      per-role gating. Wave 9 PR 6 deleted the legacy ``has_role``
      bridge, so the capability set — not ``kind`` — is now what
      gates admin tools and tool visibility.
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
        from ..core.principal_builder import (
            build_agent_bearer_principal,
            build_operator_principal,
        )

        if forwarding_operator:
            # arch-B: capabilities resolved once via the shared builder.
            # The per-project backend has no router.db handle so the
            # group-cap overlay returns empty here — the router middleware
            # (which DOES have router.db) has already resolved + admitted
            # the operator via the cookie path, so the forwarding-header
            # Principal here is purely the in-process restatement of that
            # admit.
            #
            # SEC-1: project_role is the operator's REAL signed role
            # (``forwarding_role``), NOT a fixed "operator". A viewer
            # signs role="viewer" and gets PROJECT_ROLE_BUNDLES["viewer"]
            # (read-only); an operator signs role="operator" and gets
            # the full write bundle. has_capability's project-membership
            # gate still admits resource caps because project_role is
            # non-None for both tiers. If the header verified but
            # carried no role (shouldn't happen — verify enforces a
            # known role — but defensive), project_role stays None and
            # the operator gets only system-ungated caps, i.e. nearly
            # nothing: fail closed, never fail open to "operator".
            return build_operator_principal(
                user_id=forwarding_operator,
                kind="forwarding_header",
                project_role=forwarding_role,
                sysadmin=False,
                project_name=None,
                source_token=None,
            )
        if bearer_token:
            # arch-B: the agent_bearer block (row lookup → normalized role
            # → resolve_capabilities → Principal) lives once in
            # ``core.principal_builder``. This is the ONLY caller that
            # needs the wake-loop eligibility lookup, so it opts in via
            # ``resolve_wake_loop=True``; the other three bearer sites
            # leave it off (their historical ``can_wake_loop=False``).
            return build_agent_bearer_principal(
                bearer_token, resolve_wake_loop=True
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
       is carried forward on the per-request ``Principal`` (built at
       step 4) for downstream handlers + audit logs — NOT on a
       process-wide global (SEC round-4 AC-race). On a
       present-but-invalid header (wrong HMAC, expired, malformed) the
       request is rejected with 401 — we never silently fall through to
       the bearer-token path.

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
        forwarding_role: Optional[str] = None
        if forwarding_raw is not None:
            if _g.forwarding_hmac_key:
                verified = _fh.verify(
                    forwarding_raw, _g.forwarding_hmac_key
                )
                if verified is None:
                    # Present-but-invalid header (bad HMAC, expired,
                    # malformed, OR unknown role): reject hard. Falling
                    # through to the bearer path would let a tampered
                    # header silently downgrade auth to "whatever bearer
                    # was attached", which is the wrong defaults-secure
                    # behaviour.
                    return _build_unauthorized_response(token)
                # SEC-1: carry the operator's REAL signed role through
                # to the Principal so a viewer gets viewer caps, not the
                # operator bundle. The role is HMAC-covered, so a value
                # that reaches here is one the router legitimately
                # signed for this operator's project membership.
                # SEC round-4 (AC-race): the operator identity is carried
                # forward ONLY via the per-request ``Principal`` built
                # below (stashed on ``request.state`` + the
                # ``request_principal`` ContextVar, both copy-per-task and
                # race-safe). We deliberately do NOT stamp a process-wide
                # global here — doing so let a second concurrent
                # forwarding request clobber this one's audit identity.
                forwarding_operator, forwarding_role = verified
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
            # non-terminated rows. ``_bearer_is_active`` is the single
            # definition of that predicate, shared with the GET /mcp SSE
            # pump's per-heartbeat self-validation (AC-R29-1).
            authenticated = bool(forwarding_operator) or _bearer_is_active(
                token
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
            forwarding_role=forwarding_role,
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


def _effective_catalog_principal():
    """Resolve the Principal every MCP catalog surface filters on.

    The MCP wire handlers are bare framework callbacks with no Request
    handle, so they read the per-request Principal from
    :data:`request_principal` (stamped by ``AuthHeaderMiddleware``).
    In-process / test callers may stamp only :data:`request_auth_token`
    (the bearer); for them we build the ``agent_bearer`` Principal
    locally so the role resolves the same way production would. Returns
    ``None`` when neither is set (anonymous).
    """
    principal = request_principal.get()
    if principal is not None:
        return principal
    try:
        bearer = request_auth_token.get()
    except LookupError:
        bearer = None
    if bearer:
        return _build_principal_from_request(
            request=None,
            bearer_token=bearer,
            forwarding_operator=None,
        )
    return None


@mcp_app_instance.list_tools()
async def mcp_list_tools_handler() -> List[mcp_types.Tool]:
    """MCP endpoint to list available tools."""
    return await list_available_tools(principal=_effective_catalog_principal())


def _principal_role() -> str:
    """Resolve the calling Principal's MCP-catalog role.

    arch-r3 #1+5 PR-B: a thin adapter over the single
    :func:`agent_mcp.core.principal_builder.catalog_role` so this
    surface (prompts/list + prompts/get) resolves the SAME role
    tools/list and resources do for a given Principal. The
    SEC-1 viewer→worker mapping and the sysadmin→admin / operator→admin
    mapping now live in that one function.
    """
    from ..core.principal_builder import catalog_role

    return catalog_role(_effective_catalog_principal())


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
    `resources/read` time. Unauthenticated callers — and operator /
    forwarding-header callers, who carry no per-agent inbox — see an
    empty list.

    arch-r3 #1+5 PR-B: the role is derived by the shared
    :func:`agent_mcp.core.principal_builder.catalog_role` (as for
    tools/list + prompts), not a bare ``agent_id == "admin"`` string
    test. The URI is scoped by the caller's own ``agent_id``.
    """
    from ..core.principal_builder import catalog_role
    from ..resources import resource_registry
    from pydantic_core import Url

    principal = _effective_catalog_principal()
    agent_id = principal.agent_id if principal is not None else None
    if not agent_id:
        return []

    role = catalog_role(principal)
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
    resources (operational visibility) — determined by the shared
    :func:`agent_mcp.core.principal_builder.catalog_role` (arch-r3
    #1+5 PR-B), not a bare ``agent_id == "admin"`` string test.
    """
    from ..resources import resource_registry
    from mcp.server.lowlevel.helper_types import ReadResourceContents

    uri_str = str(uri)
    token = request_auth_token.get()
    # `read()` raises ValueError on auth mismatch / unknown URI —
    # the framework surfaces the message verbatim as a JSON-RPC error.
    # Thread the resolved Principal so the cross-agent admin gate uses
    # the same catalog_role every other surface does.
    text = resource_registry.read(
        uri_str, token, principal=_effective_catalog_principal()
    )

    entry = resource_registry.find_by_uri(uri_str)
    mime = entry.meta.mime_type if entry else "application/json"
    return [ReadResourceContents(content=text, mime_type=mime)]


@mcp_app_instance.call_tool(validate_input=False)
async def mcp_call_tool_handler(name: str, arguments: dict) -> mcp_types.CallToolResult:
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

    Finding AS-1 (round 3): this handler is the single authority that
    sets ``isError`` on the wire for RETURNED results. ``dispatch_tool_call``
    RETURNS a typed :data:`ToolResult` (the REST adapter matches on the
    variant, so it must return, not raise); we consult
    :func:`is_error_result` and build a ``CallToolResult`` with
    ``isError`` set explicitly. A RETURNED denial therefore reaches the
    client with ``isError=True`` — the same fidelity the framework's
    ``_make_error_result`` gives a RAISED ``AuthRejected`` (which still
    propagates as an exception through the framework's own error path).
    """
    from ..core.tool_result import render_as_text_content, is_error_result

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
    # SD-R7-1: ``dispatch_tool_call`` RE-RAISES two controlled exception
    # types whose message is deliberate, safe client feedback —
    # ``AuthRejected`` ("Unauthorized: …") and ``ToolInputValidationError``
    # ("Input validation error: …"). We let those propagate so the MCP
    # SDK's ``_make_error_result(str(e))`` renders their intended message
    # with ``isError=True`` (round-3/4 fidelity). ANY OTHER exception is
    # an UNCAUGHT tool-body failure (sqlite3/SQLAlchemy error, KeyError,
    # OSError with a path) whose ``str(e)`` leaks table/column names,
    # paths, and internals — the SDK would reflect it verbatim to any
    # worker/manager bearer. Catch it here, log the detail server-side
    # only, and return a generic ``isError=True`` result instead.
    from ..core.authorize import AuthRejected
    from ..tools.registry import ToolInputValidationError

    try:
        result = await dispatch_tool_call(name, arguments, principal=principal)
    except (AuthRejected, ToolInputValidationError):
        # Controlled, non-sensitive messages — let the framework render
        # them (isError=True). Do NOT genericize.
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error executing tool {name!r}: {e}",
            exc_info=True,
        )
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="Tool execution failed")],
            isError=True,
        )
    # Ambient unread-message nudge (agent bearers only). Appends a single
    # advisory line to the outgoing text when the calling agent has unread
    # messages, so agents notice + read their inbox during normal work
    # without polling. Agent-only, additive, fail-safe, and skipped on the
    # ``get_agent_messages`` read tool itself — see ``core.unread_nudge``.
    from ..core.unread_nudge import maybe_append_unread_nudge

    content = maybe_append_unread_nudge(
        render_as_text_content(result),
        principal=principal,
        tool_name=name,
    )
    return mcp_types.CallToolResult(
        content=content,
        isError=is_error_result(result),
    )


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


#: Terse, detail-free ``message`` strings the sanitizer emits, keyed by
#: JSON-RPC error code. The sanitizer rebuilds every ``error`` envelope
#: from this map, so any code NOT listed here falls back to the generic
#: -32603 "Internal error" text — no exception channel can leak an
#: internal string through an unrecognised code.
_JSONRPC_TERSE_MESSAGES: dict[int, str] = {
    -32700: "Parse error",
    -32600: "Invalid Request",
    -32602: "Invalid params",
    -32603: "Internal error",
}


def _sanitize_jsonrpc_error_body(raw: bytes) -> Optional[bytes]:
    """Rebuild a JSON-RPC error envelope with a fixed, detail-free
    ``message``; ``None`` to leave the body untouched (not JSON-RPC).

    SEC-1: the MCP SDK serialises uncaught server-side detail straight
    into the JSON-RPC error ``message`` it sends the client — a
    malformed-but-parseable POST (e.g. ``method`` as an int) yields the
    full pydantic ``ValidationError`` (``input_value=…``,
    ``errors.pydantic.dev`` URLs, internal field names), and the SDK's
    catch-all emits any uncaught exception as
    ``{"code":-32603,"message":"Error handling POST request: <str(err)>"}``
    (a deep-nested-JSON body even makes that ``str(err)`` a live
    ``RecursionError`` message). All of it discloses server internals
    — library, version, schema shape, stack detail.

    The earlier SEC-1 pass sniffed for four leak-marker substrings and
    only rewrote on a hit; any exception string lacking all four passed
    through verbatim. This is now an UNCONDITIONAL fixed-envelope
    rebuild: for any parseable JSON-RPC ``error`` shape we replace
    ``error.message`` with the terse text keyed off ``error.code`` (a
    schema-validation ``-32602`` is remapped to ``-32600`` Invalid
    Request; an unrecognised code falls back to ``-32603`` Internal
    error). ``jsonrpc``/``id`` are preserved so the response stays a
    well-formed JSON-RPC error the client can parse. No marker sniff
    remains, so no exception channel can leak.

    The wrapper only feeds status-``>= 400`` ``application/json``
    responses here (SSE 2xx tool streams are never touched), so a
    healthy tool result is never rebuilt.
    """
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if not isinstance(err, dict):
        return None

    code = err.get("code")
    # A schema-validation failure means the request object was invalid
    # → -32600 Invalid Request. A recognised code keeps its own terse
    # message; anything else (incl. the SDK's -32603 catch-all and any
    # unmapped/absent code) collapses to -32603 Internal error.
    if code == -32602:
        new_code = -32600
    elif isinstance(code, int) and code in _JSONRPC_TERSE_MESSAGES:
        new_code = code
    else:
        new_code = -32603
    payload["error"] = {
        "code": new_code,
        "message": _JSONRPC_TERSE_MESSAGES[new_code],
    }
    return json.dumps(payload).encode("utf-8")


class _JsonRpcErrorSanitizer:
    """Wrap an ASGI ``send`` to terse-ify leaky JSON-RPC error bodies.

    SEC-1 fold-in. Only touches responses that are BOTH ``status >=
    400`` AND ``application/json`` — the transport-level JSON-RPC error
    envelopes the SDK emits for malformed requests. Successful tool
    calls stream as ``text/event-stream`` and pass straight through, so
    the SSE fan-out is never buffered.

    The ``http.response.start`` message is held until the (small) error
    body is fully buffered so a rewrite can recompute ``Content-Length``
    before either message goes to the wire.
    """

    def __init__(self, send) -> None:
        self._send = send
        self._intercept = False
        self._start: Optional[dict] = None
        self._body = bytearray()

    async def __call__(self, message) -> None:
        mtype = message.get("type")
        if mtype == "http.response.start":
            status = message.get("status", 200)
            ctype = b""
            for k, v in message.get("headers") or []:
                if k.lower() == b"content-type":
                    ctype = v.lower()
                    break
            if status >= 400 and ctype.startswith(b"application/json"):
                # Buffer: hold the start until the body is rewritten so
                # Content-Length stays correct.
                self._intercept = True
                self._start = message
                return
            await self._send(message)
            return
        if mtype == "http.response.body" and self._intercept:
            self._body.extend(message.get("body", b"") or b"")
            if message.get("more_body", False):
                return
            raw = bytes(self._body)
            replacement = _sanitize_jsonrpc_error_body(raw)
            out = replacement if replacement is not None else raw
            start = self._start or {"type": "http.response.start", "status": 400}
            headers = [
                (k, v)
                for k, v in (start.get("headers") or [])
                if k.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(out)).encode("ascii")))
            new_start = dict(start)
            new_start["headers"] = headers
            await self._send(new_start)
            await self._send(
                {"type": "http.response.body", "body": out, "more_body": False}
            )
            return
        await self._send(message)


# --- MCP pre-parse recursion-depth guard (pentest R1-F2) ------------
#
# Every other JSON-parse surface in this codebase wraps its `json.loads`
# in `except (ValueError, RecursionError)` and turns a deeply-nested body
# into a clean 400: `router/app.py::_parse_json_body`,
# `router/admin_users_api.py::_json_body`,
# `utils/json_utils.py::get_sanitized_json_body`. The `/mcp` transport was
# the one surface left unguarded: the MCP SDK's own
# `streamable_http.py::_handle_post_request` only catches
# `json.JSONDecodeError` around its `json.loads(body)` — `RecursionError`
# is a `RuntimeError`, not a `ValueError` subclass, so it escapes uncaught
# and turns into an HTTP 500 (identical input returns 400 on every
# REST/router path). `_JsonRpcErrorSanitizer` above scrubs the resulting
# error *message* but preserves the 500 *status*, so it can't fix this —
# the SDK has already blown its stack by the time the sanitizer sees the
# response.
#
# The fix has to sit in front of the SDK's parse, not patch its output:
# drain the body ourselves and reject an over-deeply-nested body with a
# clean 400 before the SDK ever touches the bytes. We do NOT rely on
# `json.loads` raising `RecursionError` for this — Python 3.14's json no
# longer raises on deep nesting (it parses it), so that would be a version-
# fragile guard. Instead `_body_nesting_exceeds` byte-scans the bracket
# depth (version-independent, short-circuiting). A merely-malformed (not
# deeply-nested) body is deliberately left alone — the SDK turns that into
# its own well-formed -32700 response — so the drained bytes are replayed
# to it unchanged via a synthetic `receive`.
_MCP_DEPTH_GUARD_BODY = json.dumps({
    "jsonrpc": "2.0",
    "id": "server-error",  # mirrors the SDK's own id for parse-stage errors
    "error": {"code": -32700, "message": _JSONRPC_TERSE_MESSAGES[-32700]},
}).encode("utf-8")


async def _drain_body(receive) -> tuple[bytes, bool]:
    """Fully drain an ASGI HTTP request body via `receive`.

    Returns ``(body_bytes, disconnected)``. ``disconnected`` is True if
    the client hung up mid-upload (an `http.disconnect` arrived before
    `more_body: False`) — the same signal Starlette's own
    `Request.stream()` watches for and turns into `ClientDisconnect`.
    """
    body = bytearray()
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return bytes(body), True
        body.extend(message.get("body", b"") or b"")
        if not message.get("more_body", False):
            return bytes(body), False


#: Max JSON bracket-nesting depth accepted on the /mcp transport. Legit MCP
#: payloads are shallow; anything past this is a nesting-DoS probe. Chosen
#: well above any real payload and well below the depth that blows a parser.
_MCP_MAX_BODY_NESTING_DEPTH = 1000


def _body_nesting_exceeds(body: bytes, limit: int) -> bool:
    """True if the JSON `body`'s bracket-nesting depth exceeds `limit`.

    A version-independent pre-parse guard: Python 3.14's ``json.loads`` no
    longer raises ``RecursionError`` on a deeply-nested body (it parses it),
    so relying on catching that error is not portable. This scans the raw
    bytes, counting ``[``/``{`` depth (ignoring brackets inside string
    literals), and short-circuits as soon as the limit is crossed — so a
    hostile 50k-deep body costs ``limit`` iterations, not a full parse.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in body:  # iterating bytes yields ints
        if in_string:
            if escaped:
                escaped = False
            elif ch == 0x5C:  # backslash
                escaped = True
            elif ch == 0x22:  # closing quote
                in_string = False
            continue
        if ch == 0x22:  # opening quote
            in_string = True
        elif ch == 0x5B or ch == 0x7B:  # [ or {
            depth += 1
            if depth > limit:
                return True
        elif ch == 0x5D or ch == 0x7D:  # ] or }
            if depth > 0:
                depth -= 1
    return False


def _replay_receive(raw_receive, body: bytes, disconnected: bool):
    """Build an ASGI `receive` callable that replays an already-drained
    body exactly once, then delegates to the real channel.

    `receive` is a single-use channel — the depth guard in
    `_McpAsgiApp.__call__` has to drain it fully to check nesting depth
    before the MCP SDK's own request parsing ever sees it, so the SDK is
    handed this replay instead of the (now-exhausted) live channel. A
    mid-upload disconnect is replayed as `http.disconnect` (rather than
    the partial body) so Starlette's `Request.stream()` still raises
    `ClientDisconnect` at the same point it would have without this guard
    in front of it.

    After the first (replayed) event, subsequent reads DELEGATE to the
    real `raw_receive` channel — the SDK reads `receive` more than once
    (e.g. to observe `http.disconnect` after consuming the body), so
    returning a synthetic disconnect on the second call would break the
    normal request lifecycle. The single drained body event is replayed
    once; everything after it comes from the live channel unchanged.
    """
    sent = False

    async def _receive():
        nonlocal sent
        if not sent:
            sent = True
            if disconnected:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": body, "more_body": False}
        # Past the first call: hand back the real channel's events.
        return await raw_receive()

    return _receive


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
        if scope.get("type") == "http" and scope.get("method") == "POST":
            # Pre-parse recursion-depth guard (pentest R1-F2) — see the
            # comment block above `_drain_body`. Only POST reaches the
            # SDK's own `json.loads(body)`; DELETE never reads a body
            # (stateless mode returns 405 for it) and GET is handled
            # entirely in-house above, so neither needs this.
            raw_receive = receive
            body, disconnected = await _drain_body(raw_receive)
            if not disconnected and _body_nesting_exceeds(
                body, _MCP_MAX_BODY_NESTING_DEPTH
            ):
                # Over-deep body → clean 400 before the SDK parses it.
                # A merely-malformed (not deeply-nested) body is left
                # for the SDK's own -32700 handling — its exact bytes
                # are replayed unchanged via the synthetic `receive`.
                await _send_simple_response(send, 400, _MCP_DEPTH_GUARD_BODY)
                return
            receive = _replay_receive(raw_receive, body, disconnected)
        # POST/DELETE → SDK. Wrap ``send`` so a malformed-request
        # JSON-RPC error envelope (which the SDK fills with a raw
        # pydantic ValidationError dump) is rewritten to a terse
        # standard envelope before it reaches the client — SEC-1.
        await self._manager.handle_request(
            scope, receive, _JsonRpcErrorSanitizer(send)
        )

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
                self._pump(session_id, queue, send, bearer),
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

    async def _pump(
        self, session_id: str, queue: asyncio.Queue, send, bearer: str
    ) -> None:
        """Drain `queue` onto the SSE wire forever (until cancelled).

        One SSE `data:` frame per queue payload. Heartbeat comments
        are emitted whenever the queue stays empty past
        `_HEARTBEAT_INTERVAL_SECONDS` — they double as the dead-peer
        detector (a write to a closed socket raises here, which
        bubbles up and ends the request via the surrounding `wait`).

        We also call `session_registry.touch_session` on every
        successful payload + heartbeat so the periodic pruner doesn't
        evict still-live sessions.

        Self-validation (AC-R29-1): a GET /mcp stream authenticates its
        bearer ONCE at open, then this loop pumps indefinitely. On every
        iteration we re-check that the bearer is still live (the same
        cache-only predicate the ``/mcp`` auth gate uses). If the agent
        was terminated / the token revoked, we break so the surrounding
        `_handle_get` tears the stream down — the push channel never
        trusts its open-time auth beyond one heartbeat interval, so
        revocation is complete across this channel too, not just the
        request path. `terminate_agent` also enqueues a
        ``CLOSE_STREAM`` sentinel to wake this loop immediately rather
        than waiting for the next heartbeat tick.
        """
        while True:
            # Re-validate before every emit — teardown on revocation.
            if not _bearer_is_active(bearer):
                logger.info(
                    "session_registry: bearer revoked — closing GET /mcp "
                    "stream session=%s", session_id,
                )
                return
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

            # Active teardown wake (AC-R29-1): loop back so the
            # top-of-loop self-validation runs now and returns if the
            # bearer is revoked, instead of serialising the sentinel
            # onto the wire.
            if payload is session_registry.CLOSE_STREAM:
                continue

            # Re-validate again post-dequeue: a data payload may have
            # been queued BEFORE revocation and only reach the front
            # of the FIFO after CLOSE_STREAM was enqueued behind it
            # (or before revocation happened at all). Without this
            # check the loop would still wire-write one already-queued
            # payload to a bearer that left `active_agents` between
            # the top-of-loop check and here.
            if not _bearer_is_active(bearer):
                logger.info(
                    "session_registry: bearer revoked — discarding "
                    "queued payload, closing GET /mcp stream "
                    "session=%s", session_id,
                )
                return

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


def _bearer_is_active(bearer: str) -> bool:
    """True iff ``bearer`` maps to a live (non-terminated) agent.

    Cache-only predicate: ``state.active_agents`` holds only
    non-terminated rows — terminate evicts post-commit and the repo
    never warms a terminated row back in (see
    ``test_terminate_token_revocation_cache``) — so a terminated /
    revoked bearer is absent and this reads ``False`` without a DB
    roundtrip. This is the SAME liveness check the ``/mcp`` auth gate
    and the per-request tool-dispatch path rely on; the GET /mcp SSE
    pump re-checks it every heartbeat so a stream opened BEFORE
    revocation is torn down rather than surviving it (AC-R29-1).

    arch-r5 #7: this is the token-keyed sibling of
    :meth:`agent_mcp.repositories.agent_repository.AgentRepository.active_agent_ids`
    (the agent_id-keyed owner used by ``view_status`` / worker-to-worker
    messaging). Both read the identical ``state.active_agents`` dict —
    one bearer at a time here, the full id-set there — so "is this
    bearer active" and "which agents are active" can never disagree.
    """
    from ..core import globals as _g

    return bool(bearer) and bearer in _g.active_agents


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
            # VULN-001 (security audit 2026-06-29): the wildcard ``*``
            # entry was removed. Browsers treat
            # ``Access-Control-Allow-Origin: *`` paired with
            # ``Access-Control-Allow-Credentials: true`` as a CSRF
            # vector — any attacker-controlled origin could issue
            # credentialed requests against this server using a
            # logged-in operator's session cookie. The allowlist is
            # now sourced from :data:`ALLOWED_ORIGINS` so the
            # CORSMiddleware and the per-route ``handle_options``
            # fallback share a single source of truth.
            allow_origins=sorted(ALLOWED_ORIGINS),
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
