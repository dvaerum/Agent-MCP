# Agent-MCP/mcp_template/mcp_server_src/app/main_app.py
import json
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from starlette.applications import Starlette
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
from ..core.auth import verify_token
from .routes import routes as http_routes
from .server_lifecycle import application_startup, application_shutdown
from ..tools.registry import list_available_tools, dispatch_tool_call, request_auth_token


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


class AuthHeaderMiddleware(BaseHTTPMiddleware):
    """Capture Authorization: Bearer into request_auth_token + gate /mcp.

    Two responsibilities:

    1. Bind any incoming `Authorization: Bearer <tok>` value to the
       `request_auth_token` ContextVar so tool dispatch can fall back
       to it when JSON-RPC arguments don't carry a `token` field. This
       is the same fallback the prior SSE transport relied on; the
       Streamable HTTP transport reads from the same ContextVar.

    2. Gate `/mcp` at the HTTP layer. POST/GET/DELETE on `/mcp` MUST
       carry a valid bearer (admin token or active-agent token). An
       MCP-protocol JSON-RPC error inside a 200 response is the wrong
       shape for an *unauthenticated* request — there isn't a JSON-RPC
       envelope to wrap, the caller hasn't even authenticated to the
       transport yet. We reject with 401 + a tiny JSON body so the
       client knows immediately what to fix.

       Note: per-tool role checks (admin vs worker) still happen
       inside the tool layer via `@requires`/`@requires_policy`. This
       middleware only enforces "is this *any* valid token?".
    """

    async def dispatch(self, request, call_next):
        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            request_auth_token.set(token)

        path = request.url.path
        # Gate the MCP transport endpoint. We match exact `/mcp` and
        # `/mcp/...` so a future sub-path doesn't accidentally bypass
        # auth. `/api/*` and the dashboard routes keep their own
        # per-route token handling.
        if path == "/mcp" or path.startswith("/mcp/"):
            if not token or not (
                verify_token(token, required_role="admin")
                or verify_token(token, required_role="agent")
            ):
                from starlette.responses import JSONResponse

                return JSONResponse(
                    {
                        "error": "unauthorized",
                        "hint": (
                            "Send Authorization: Bearer <admin-or-agent-token> "
                            "on POST /mcp."
                        ),
                    },
                    status_code=401,
                )

        return await call_next(request)


# --- MCP Server Setup ----------------------------------------------
mcp_app_instance = MCPLowLevelServer("mcp-server")


@mcp_app_instance.list_tools()
async def mcp_list_tools_handler() -> List[mcp_types.Tool]:
    """MCP endpoint to list available tools."""
    return await list_available_tools()


def _caller_role() -> str:
    """Resolve the calling bearer's role for visibility filtering.

    Mirrors the resolver in `tools.registry.list_available_tools` —
    extracted here so the prompts + tools handlers stay in lockstep
    on what "admin" vs "worker" vs "anonymous" mean. Failures fall
    back to "anonymous" (most conservative).
    """
    try:
        bearer = request_auth_token.get()
    except LookupError:
        bearer = None
    if not bearer:
        return "anonymous"
    try:
        from ..core.auth import verify_token, get_agent_id

        if verify_token(bearer, "admin"):
            return "admin"
        if get_agent_id(bearer):
            return "worker"
    except Exception as e:
        logger.warning(
            "prompts/list: failed to resolve bearer to role (%s); "
            "treating as anonymous.",
            e,
        )
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

    role = _caller_role()
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

    role = _caller_role()
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

    `validate_input=False` disables the framework's automatic
    `jsonschema.validate(arguments, tool.inputSchema)` step
    (mcp/server/lowlevel/server.py:497). We re-run validation inside
    `dispatch_tool_call` *after* cleaning arguments for the real-world
    shapes LLM clients produce (`token: null`, leaked `_meta`,
    integer-as-string). See `_clean_arguments_for_schema` in
    `tools/registry.py` and the tolerance suite in
    `tests/test_call_tool_argument_tolerance.py`.
    """
    return await dispatch_tool_call(name, arguments)


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
    """

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope, receive, send):
        await self._manager.handle_request(scope, receive, send)


# --- Starlette Application Creation --------------------------------
def create_app(project_dir: str, admin_token_cli: Optional[str] = None) -> Starlette:
    """Build and configure the main Starlette application.

    Lifespan: starlette >= 0.45 uses a single `lifespan` async context
    manager (the on_startup/on_shutdown kwargs are gone). We chain the
    app's own startup/shutdown with the StreamableHTTP session
    manager's `.run()` context — the SDK requires `.run()` to wrap
    request handling, otherwise the manager's task group is not
    initialised and `handle_request` raises RuntimeError.
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
    async def lifespan(_app: Starlette):
        await application_startup(
            project_dir_path_str=project_dir, admin_token_param=admin_token_cli
        )
        logger.info(
            "Starlette app startup complete. Background tasks should be started by the server runner."
        )
        # `manager.run()` must wrap any request handling — it creates
        # the task group that spawns per-request server tasks.
        async with manager.run():
            try:
                yield
            finally:
                await application_shutdown()
                logger.info("Starlette app shutdown complete.")

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

    all_routes = list(http_routes)

    # New Streamable HTTP transport at /mcp (spec rev 2025-03-26).
    all_routes.append(Mount('/mcp', app=_McpAsgiApp(manager), name="mcp_transport"))

    # Legacy endpoints — 410 Gone with the migration JSON body. Kept
    # mounted (rather than deleted outright) so any client/router
    # still pointed at the old shape gets a structured, parseable hint
    # rather than a bare 404. Remove these mounts in a later major
    # version once telemetry shows no traffic.
    all_routes.append(Mount('/sse', app=_GoneApp(), name="legacy_sse_gone"))
    all_routes.append(Mount('/messages', app=_GoneApp(), name="legacy_messages_gone"))

    app = Starlette(
        routes=all_routes,
        lifespan=lifespan,
        middleware=middleware_stack,
        debug=os.environ.get("MCP_DEBUG", "false").lower() == "true",
    )

    logger.info("Starlette application instance created with routes and lifecycle events.")
    return app

# The actual running of the app (e.g., with uvicorn) will be handled by cli.py
