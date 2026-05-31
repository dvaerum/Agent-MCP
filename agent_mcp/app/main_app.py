# Agent-MCP/mcp_template/mcp_server_src/app/main_app.py
import uuid
import datetime # For SSE connection logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional # Added List and Optional
import os # Added os import

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware import Middleware # If any middleware is needed
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware # Example if CORS is needed

# MCP Server specific imports
from mcp.server.lowlevel import Server as MCPLowLevelServer # Renamed to avoid conflict
from mcp.server.sse import SseServerTransport
import mcp.types as mcp_types # For MCP tool types

# Project-specific imports
from ..core.config import logger
from ..core import globals as g # For g.connections (if still used for SSE tracking)
from .routes import routes as http_routes # Import defined HTTP routes
from .server_lifecycle import application_startup, application_shutdown, start_background_tasks
from ..tools.registry import list_available_tools, dispatch_tool_call, request_auth_token


class AuthHeaderMiddleware(BaseHTTPMiddleware):
    """Q6e: capture Authorization: Bearer into request_auth_token.

    Tool dispatch reads request_auth_token as a fallback when the
    JSON-RPC arguments don't contain `token`. This lets MCP clients
    that speak standard HTTP bearer auth (e.g. Claude Code via
    `claude mcp add --header`) authenticate without the body-rewriting
    workaround the router currently maintains.
    """

    async def dispatch(self, request, call_next):
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            request_auth_token.set(auth[7:].strip())
        return await call_next(request)

# --- MCP Server Setup (mimicking original main.py:2055) ---
mcp_app_instance = MCPLowLevelServer("mcp-server") # Name from original main.py:2055

# Register MCP tool handlers with the low-level server instance
# Original main.py: lines 1636-1938 (@app.list_tools, @app.call_tool)
@mcp_app_instance.list_tools()
async def mcp_list_tools_handler() -> List[mcp_types.Tool]:
    """MCP endpoint to list available tools."""
    return await list_available_tools() # Calls the function from tools.registry

@mcp_app_instance.call_tool()
async def mcp_call_tool_handler(name: str, arguments: dict) -> List[mcp_types.TextContent]:
    """MCP endpoint to call a specific tool."""
    # `dispatch_tool_call` from tools.registry handles sanitization and routing
    return await dispatch_tool_call(name, arguments)


# --- SSE Transport Setup (mimicking original main.py:1943-1969 for SSE part) ---
# The SseServerTransport handles /messages/ (POST for tool calls) and /sse (GET for connections)
sse_transport = SseServerTransport("/messages/") # Path from original main.py:1943

async def sse_connection_handler(request): # Starlette Request object
    """Handles new SSE client connections."""
    try:
        # Client ID generation (original main.py:1947)
        # While SseServerTransport might manage its own client IDs, logging this is useful.
        client_id_log = str(uuid.uuid4())[:8] # For logging this specific connection attempt
        client_host = request.client.host if request.client else 'unknown'
        logger.info(f"SSE connection request from {client_host} (Log ID: {client_id_log})")
        # The original also printed to console, which logger now handles.
        # print(f"[{datetime.datetime.now().isoformat()}] SSE connection request from {client_host} (ID: {client_id_log})")

        # `connect_sse` is a context manager from SseServerTransport
        # Extract ASGI components from Starlette Request
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send # ASGI scope, receive, send callables
        ) as streams:
            # streams[0] is input_stream, streams[1] is output_stream
            actual_client_id = streams[2] if len(streams) > 2 else client_id_log # Get actual client ID if provided by transport
            logger.info(f"SSE client connected: {actual_client_id}")
            # print(f"[{datetime.datetime.now().isoformat()}] SSE client connected: {actual_client_id}")
            
            # Store connection if g.connections is still used for tracking (original main.py:147)
            # g.connections[actual_client_id] = {"connected_at": datetime.datetime.now().isoformat()}

            try:
                # Run the MCP low-level server for this connection
                # (Original main.py:1957-1961)
                await mcp_app_instance.run(
                    streams[0], # input_stream
                    streams[1], # output_stream
                    mcp_app_instance.create_initialization_options() # As per original
                )
            finally:
                logger.info(f"SSE client disconnected: {actual_client_id}")
                # print(f"[{datetime.datetime.now().isoformat()}] SSE client disconnected: {actual_client_id}")
                # if actual_client_id in g.connections:
                #     del g.connections[actual_client_id]
    except Exception as e:
        # Log errors during SSE connection handling (original main.py:1964-1966)
        logger.error(f"Error in SSE connection handler: {str(e)}", exc_info=True)
        # print(f"[{datetime.datetime.now().isoformat()}] Error in SSE connection: {str(e)}")
        # Starlette will handle sending an error response if one isn't already sent.
        raise # Re-raise to let Starlette handle it if appropriate


# --- Starlette Application Creation ---
def create_app(project_dir: str, admin_token_cli: Optional[str] = None) -> Starlette:
    """
    Creates and configures the main Starlette application.
    """
    # Lifecycle: starlette >= 0.45 removed the on_startup/on_shutdown
    # kwargs in favor of a single `lifespan` async context manager.
    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await application_startup(
            project_dir_path_str=project_dir, admin_token_param=admin_token_cli
        )
        logger.info(
            "Starlette app startup complete. Background tasks should be started by the server runner."
        )
        try:
            yield
        finally:
            await application_shutdown()
            logger.info("Starlette app shutdown complete.")

    # Define middleware (if any)
    # Enable CORS for dashboard integration - comprehensive CORS config
    # AuthHeaderMiddleware runs first so the ContextVar is set before
    # any downstream handler (including the MCP message dispatcher).
    middleware_stack = [
        Middleware(AuthHeaderMiddleware),
        Middleware(
            CORSMiddleware,
            allow_origins=[
                'http://localhost:3847',  # Primary dashboard port
                'http://127.0.0.1:3847',  # Alternative localhost
                'http://localhost:3000',  # Next.js default
                'http://localhost:3001',  # Common alternative
                '*'  # Fallback for any other ports during development
            ],
            allow_credentials=True,
            allow_methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'HEAD', 'PATCH'],
            allow_headers=['*'],
            expose_headers=['*'],
            max_age=3600,  # Cache preflight for 1 hour
        )
    ]

    # Create the Starlette app
    # The original main.py:2100 created `web_app = Starlette()`
    # It then added routes.
    
    # The routes list from app.routes.py already contains most HTTP routes.
    # We need to add the SSE specific routes here.
    all_routes = list(http_routes) # Start with routes from app/routes.py

    # Create ASGI app wrapper for handle_post_message
    # The sse_transport.handle_post_message is already a proper ASGI callable
    # We just need to wrap it as an ASGI app class for Mount to work
    class MessageHandlerApp:
        """ASGI app wrapper for SseServerTransport.handle_post_message.

        Intercepts the bare `Could not find session` 404 that MCP's SDK
        returns (`mcp/server/sse.py:226-227`) when a POST arrives for a
        session_id the backend has no record of — typically because the
        backend restarted and the client kept POSTing with its old
        session_id. The SDK's body is one line and gives the caller no
        hint what to do; we rewrite it to spell out the fix
        (reconnect) and echo the offending session_id so operators can
        correlate with the backend log.
        """
        async def __call__(self, scope, receive, send):
            sid = ""
            qs = scope.get("query_string", b"")
            if isinstance(qs, (bytes, bytearray)):
                # Cheap query-string parse; avoid pulling urllib for
                # a single key.
                for part in qs.split(b"&"):
                    if part.startswith(b"session_id="):
                        sid = part[len(b"session_id="):].decode(errors="replace")
                        break

            captured_start: dict[str, object] = {}
            replace_body = False

            async def _send_wrapper(message):
                nonlocal replace_body
                if message["type"] == "http.response.start":
                    captured_start.update(message)
                    if message.get("status") == 404:
                        replace_body = True
                        # Hold the start until the body is known — we
                        # may need to rewrite Content-Length.
                        return
                    await send(message)
                    return
                if message["type"] == "http.response.body" and replace_body:
                    body = message.get("body", b"") or b""
                    if b"Could not find session" in body:
                        new_text = (
                            f"Could not find session {sid or '<unknown>'} — your MCP "
                            "session is no longer registered on the backend. "
                            "This is usually because the backend restarted "
                            "(deploy, OOM, manual restart) and lost in-memory "
                            "session state. Reconnect your MCP session so a "
                            "new session_id is issued, then retry. In "
                            "claude-code: `/mcp reconnect <server-name>` "
                            "(e.g. `/mcp reconnect agent-mcp`)."
                        )
                        new_body = new_text.encode("utf-8")
                        # Rewrite Content-Length on the held start
                        # frame; ASGI lowercases header names by spec.
                        headers = [
                            (k, v) for (k, v) in captured_start.get("headers", [])
                            if k.lower() != b"content-length"
                        ]
                        headers.append((b"content-length", str(len(new_body)).encode()))
                        captured_start["headers"] = headers
                        await send(captured_start)
                        await send({
                            "type": "http.response.body",
                            "body": new_body,
                            "more_body": message.get("more_body", False),
                        })
                        replace_body = False
                        return
                    # 404 but not the one we care about; flush the
                    # held start and the original body untouched.
                    await send(captured_start)
                    await send(message)
                    replace_body = False
                    return
                await send(message)

            await sse_transport.handle_post_message(scope, receive, _send_wrapper)

    # ASGI app wrapper for sse_connection_handler. Must be a Mount, not
    # a Route, because the handler streams via the raw ASGI `send`
    # callable (through sse_transport.connect_sse) and returns None
    # implicitly. Route's request_response wrapper would then do
    # `await None(scope, receive, send)` → TypeError NoneType not
    # callable, surfacing to clients as ServerDisconnectedError under
    # multi-session load. UPSTREAM_ISSUES.md issue A.
    class SseConnectApp:
        """ASGI app wrapper for sse_connection_handler.

        The handler expects a Starlette `Request`. We construct one
        from the ASGI scope/receive and pass through, then return
        without producing a `Response` — the handler has already
        streamed its events via the send callable.
        """
        async def __call__(self, scope, receive, send):
            from starlette.requests import Request

            # Starlette's Mount sets scope['root_path']='/sse' on the
            # inner app. MCP's SseServerTransport.connect_sse then
            # computes the follow-up POST URL it tells the client to use
            # as `root_path.rstrip('/') + self._endpoint`
            # (mcp/server/sse.py:152), which produces
            # `/sse/messages/?session_id=...` instead of the canonical
            # `/messages/?session_id=...`. Any reverse-proxy / router
            # rewriting `data: /messages/` on the SSE byte stream
            # (multi-tenant deployments) then stops matching, and
            # clients POST to a 404. Strip root_path so the transport
            # advertises the canonical URL — the Mount still routes
            # /sse → here either way.
            if scope.get("root_path"):
                scope = {
                    **scope,
                    "path": scope.get("root_path", "") + scope.get("path", ""),
                    "root_path": "",
                }
            request = Request(scope, receive=receive, send=send)
            await sse_connection_handler(request)

    # Add SSE routes (Original main.py:2113-2114)
    all_routes.append(Mount('/sse', app=SseConnectApp(), name="sse_connect"))
    # Add the SseServerTransport's POST message handler as a Mount with ASGI app wrapper
    all_routes.append(Mount('/messages', app=MessageHandlerApp(), name="mcp_post_message"))

    # Note: Static file serving removed - dashboard is now served separately via npm run dev
    
    # Create the Starlette application instance
    app = Starlette(
        routes=all_routes,
        lifespan=lifespan,
        middleware=middleware_stack,
        debug=os.environ.get("MCP_DEBUG", "false").lower() == "true" # Optional debug mode
    )

    logger.info("Starlette application instance created with routes and lifecycle events.")
    return app

# The actual running of the app (e.g., with uvicorn) will be handled by cli.py