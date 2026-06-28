# Agent-MCP/mcp_template/mcp_server_src/tools/registry.py
import inspect as _inspect
from dataclasses import dataclass as _dataclass
from typing import List, Dict, Any, Callable, Awaitable, Optional, Union
import mcp.types as mcp_types # Assuming this is the correct import for your mcp.types

# Import utility for JSON sanitization, as handle_tool uses it
from ..utils.json_utils import sanitize_json_input, get_sanitized_json_body
# Import the central logger
from ..core.config import logger
# Typed auth-failure exception from the decorator surface; the
# dispatcher catches it explicitly so the audit log line is uniform.
from ..core.authorize import AuthRejected
# Shared Registry[T] core (Candidate B, 2026-06-02 architecture
# review). Tools live in `tool_registry` alongside resources +
# prompts; the legacy `tool_schemas` / `tool_implementations` dicts
# below are kept as backwards-compatible mirrors so the dozens of
# tests + downstream consumers that import them keep working.
from ..core.registry import Registry, RegistryEntry
# Wave 6 PR 0 — Principal + ToolResult are the new dispatch
# vocabulary. The bridge below lets old-style tools
# (``list[TextContent]`` returns, no ``principal`` kwarg) coexist
# with new-style tools (``ToolResult`` returns, take ``principal``)
# during PRs 1-5; PR 6 removes the bridge once every tool has
# been migrated.
from ..core.principal import Principal as _Principal
from ..core.tool_result import (
    Ok as _Ok,
    ToolResult as _ToolResult,
    render_as_text_content as _render_as_text_content,
)

# Tool implementations will be imported here once they are created.
# For now, we'll define placeholders for the functions they will call.
# These will be replaced by actual imports from other tool modules.
# e.g., from .admin_tools import create_agent_tool_impl, view_status_tool_impl, ...
#       from .task_tools import assign_task_tool_impl, ...
#       from .rag_tools import ask_project_rag_tool_impl

# --- Tool Function Placeholders (to be replaced by actual imports) ---
# These represent the core logic of each tool, now separated from parsing/auth.
async def placeholder_tool_logic(*args, **kwargs) -> List[mcp_types.TextContent]:
    tool_name = kwargs.get('_tool_name', 'unknown_placeholder_tool')
    logger.warning(f"Placeholder logic called for tool: {tool_name} with args: {args}, kwargs: {kwargs}")
    return [mcp_types.TextContent(type="text", text=f"Placeholder response for {tool_name}. Not implemented in registry yet.")]

# This dictionary will map tool names to their implementation functions.
# It will be populated by importing and assigning the actual tool functions.
# Example:
# tool_implementations: Dict[str, Callable[..., Awaitable[List[mcp_types.TextContent]]]] = {
# "create_agent": create_agent_tool_impl, # from .admin_tools
# "view_status": view_status_tool_impl,   # from .admin_tools
# ... and so on for all tools
# }
# For now, it's empty and will be filled as we create the tool modules.
tool_implementations: Dict[str, Callable[..., Awaitable[List[mcp_types.TextContent]]]] = {}

# Lazy import of jsonschema so the registry can be imported in
# contexts where the dependency isn't installed (tests for the
# registration invariant don't need to validate).
try:
    import jsonschema as _jsonschema  # type: ignore
except ImportError:  # pragma: no cover
    _jsonschema = None  # type: ignore

# Top-level argument keys that real MCP clients leak into the
# `arguments` object but that schemas (with `additionalProperties:
# false`) will reject. These keys are reserved by the MCP spec at the
# *params* level (CallToolRequestParams.meta / _meta) — when clients
# put them in arguments instead, drop them silently so the call still
# reaches the tool.
_RESERVED_ARG_KEYS = frozenset({"_meta", "meta"})


def _clean_arguments_for_schema(
    arguments: Dict[str, Any], schema: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Make caller-supplied arguments tolerant of two real-world shapes
    that LLM-driven MCP clients regularly produce and that
    `jsonschema.validate(arguments, inputSchema)` would otherwise
    reject:

    1.  `{"token": null, ...}` — models serialize the optional
        `token` field as JSON null when they decide "I don't have one
        to send". Schemas declare `token` as a plain `{"type":
        "string"}` (no `null` accepted), so validation fails with
        `'None is not of type string'`. Strip any top-level key whose
        value is `None`; the dispatcher's Q6e bearer-token fallback
        will fill `token` back in from the Authorization header.

    2.  `{"_meta": {...}, ...}` — some MCP client SDKs leak the
        spec-defined `_meta` field (which belongs at the params
        level) into the arguments object. Schemas use
        `additionalProperties: false`, which rejects it. Drop the
        reserved-name keys before validation.

    Schema-driven cleanups go on top:

    3.  If the property is `{"type": "integer"}` and the value is a
        string of digits (or a Python bool, which jsonschema would
        accept as integer but most tool impls treat as the bool),
        coerce. Many LLM clients quote integer arguments.

    Returns a NEW dict; never mutates the caller's input.
    """
    cleaned: Dict[str, Any] = {}
    schema_props = (schema or {}).get("properties") or {}
    for key, value in arguments.items():
        if key in _RESERVED_ARG_KEYS:
            continue
        if value is None:
            # Treat null as absent; schema-required keys with null
            # values would also be wrong, but the dispatcher's other
            # checks (and the tool's own arg parsing) catch those.
            continue
        # Integer-as-string coercion for properties declared as integers.
        prop_schema = schema_props.get(key) or {}
        if (
            isinstance(value, str)
            and prop_schema.get("type") == "integer"
            and value.lstrip("-").isdigit()
        ):
            try:
                cleaned[key] = int(value)
                continue
            except ValueError:  # pragma: no cover
                pass
        cleaned[key] = value
    return cleaned


def _find_schema_for(tool_name: str) -> Optional[Dict[str, Any]]:
    for entry in tool_schemas:
        if entry.get("name") == tool_name:
            return entry.get("inputSchema")
    return None


# This list will hold the schema definitions for all tools.
# It will be populated by defining each tool's schema.
# Example entry:
# {
# "name": "create_agent",
# "description": "Create a new agent...",
# "inputSchema": { ... schema ... }
# }
tool_schemas: List[Dict[str, Any]] = []


# --- Shared Registry[T] adapter (Candidate B) ---------------------
#
# `ToolImpl` is the payload type each entry carries: the schema dict
# + the async implementation function. Mirrors what `tool_schemas`
# and `tool_implementations` carry today, but routed through the
# shared `Registry[T]` container so resources / prompts can use the
# same role-based visibility filtering.


@_dataclass
class ToolImpl:
    """Per-tool payload stored in the shared registry.

    PR-W1c (2026-06-05) added ``declared_visibility``: the literal
    string the caller passed to ``register_tool(..., visibility=...)``
    (``"admin"``, ``"any"``, or ``"worker-if-toggled:<keys>"``). The
    legacy ``RegistryEntry.visibility`` field stays a callable so the
    shared :class:`agent_mcp.core.registry.Registry` filter keeps
    working; the raw declaration is preserved here so
    :data:`agent_mcp.tools.access.TOOL_ACCESS` can derive the access
    table from registry introspection (kwarg + decorator
    ``_required_role``) without re-parsing source.
    """

    description: str
    input_schema: Dict[str, Any]
    implementation: Callable[..., Awaitable[List[mcp_types.TextContent]]]
    declared_visibility: str = "any"


class ToolRegistry(Registry[ToolImpl]):
    """Tool subsystem adapter for the shared Registry.

    Adds `dispatch(name, arguments)` as the tools' verb. The schema
    + impl live in `entry.meta`; visibility is a callable that
    consults `tools.access.is_visible_to_role` so every existing
    classification (admin / any / worker-if-toggled:<key>) continues
    to govern `tools/list` filtering.
    """

    async def dispatch(
        self, name: str, arguments: Dict[str, Any]
    ) -> List[mcp_types.TextContent]:
        """Look up the tool by name and invoke its implementation
        with the (already-cleaned) arguments dict. Re-raises every
        framework-relevant exception (AuthRejected, validation
        errors) so the MCP framework's `_make_error_result` builds
        the correct `isError=True` response.
        """
        entry = self.get(name)
        if entry is None:
            raise ValueError(f"Unknown tool: {name}")
        return await entry.meta.implementation(arguments)


#: The single tool registry consumed by `mcp_call_tool_handler` +
#: `mcp_list_tools_handler` in `app/main_app.py`. Populated lazily
#: by `register_tool` as each tool module is imported.
tool_registry: ToolRegistry = ToolRegistry()


def _tool_visibility_policy(tool_name: str) -> Callable[[str], bool]:
    """Build a visibility callable that defers to the access table.

    Captured at registration time so the entry doesn't need to know
    its own name; the closure carries it through to `list_visible`.
    The lookup is intentionally lazy (imported inside the callable)
    so this module stays importable in tests that don't load the
    access table.
    """

    def _policy(role: str) -> bool:
        try:
            from .access import is_visible_to_role
        except Exception:
            # Fail-open like `list_available_tools` does — surface
            # the tool rather than silently hide it on a config bug.
            return True
        return is_visible_to_role(tool_name, role)

    return _policy


# --- Core Tool Registry Functions ---

def register_tool(
    name: str,
    description: str,
    input_schema: Dict[str, Any],
    implementation: Callable[..., Awaitable[List[mcp_types.TextContent]]],
    *,
    visibility: str = "any",
):
    """
    Registers a tool's schema and its implementation.
    This function will be called by each tool module to register itself.

    ``visibility`` (PR-W1c, 2026-06-05): one of ``"admin"``, ``"any"``,
    or ``"worker-if-toggled:<key>[,<key>...]"``. Surfaces the access
    policy at the registration site so the registry / ``tools/list``
    filter / UI can introspect it without re-reading source. Defaults
    to ``"any"`` (matches the pre-PR-W1c implicit default in
    ``is_visible_to_role`` — unclassified tools are visible to
    everyone).

    Enforcement of the policy lives at the call site in the impl's
    ``@requires_role(...)`` / ``@requires_policy(...)`` decorator —
    the kwarg here is metadata for the visibility filter, not the
    auth gate. See :mod:`agent_mcp.tools._access` for the decorator
    and the double-source-of-truth rationale.
    """
    global tool_schemas, tool_implementations

    # Check for duplicate tool names
    if name in tool_implementations:
        logger.warning(f"Tool '{name}' is being re-registered. Overwriting previous definition.")

    tool_schemas.append({
        "name": name,
        "description": description,
        "inputSchema": input_schema
        # mcp.types.Tool in the original also had an outputSchema, which can be added if needed.
    })
    tool_implementations[name] = implementation

    # Mirror the registration into the shared Registry[T]. The
    # callable visibility still defers to
    # `tools.access.is_visible_to_role` (which itself derives from
    # registry introspection post-W1c); the raw declared visibility
    # is stored on the meta payload so the derivation can read it
    # without re-parsing.
    tool_registry.register(
        RegistryEntry(
            name=name,
            visibility=_tool_visibility_policy(name),
            meta=ToolImpl(
                description=description,
                input_schema=input_schema,
                implementation=implementation,
                declared_visibility=visibility,
            ),
        )
    )

    logger.info(f"Registered tool: {name}")


async def list_available_tools() -> List[mcp_types.Tool]:
    """
    Returns a list of available tools with their schemas.
    This replaces the logic from `@app.list_tools()` in main.py (lines 1636-1858).
    It now reads from the `tool_schemas` list populated by `register_tool`.

    The list is filtered by the calling bearer's role (admin / worker /
    anonymous) per `agent_mcp.tools.access.is_visible_to_role`. The
    bearer is taken from the `request_auth_token` ContextVar (set by
    the HTTP middleware on each incoming request); in-process callers
    that haven't set the ContextVar are treated as anonymous and see
    only "any"-classified tools.

    Phase 7g: previously this returned every registered tool to every
    caller. That worked while the router's `_rewrite_tools_list_event`
    hid admin-only tools, but Phase 7f removed that rewrite (per Q7.1:
    no MCP-protocol manipulation in the router). Without backend-side
    filtering, workers saw the full catalogue, attempted admin tools,
    and got isError=true (PR #15) — wasting tokens and confusing the
    model.
    """
    # Resolve calling bearer to a role string. Imported lazily so the
    # registry module stays importable in contexts where the access
    # table or auth helpers haven't been wired (e.g. some unit tests).
    role = "anonymous"
    try:
        bearer = request_auth_token.get()
    except LookupError:
        bearer = None

    if bearer:
        try:
            from ..core.auth import verify_token, get_agent_id

            if verify_token(bearer, "admin"):
                role = "admin"
            elif get_agent_id(bearer):
                role = "worker"
        except Exception as e:
            # Don't fail the whole tools/list on an auth-resolver bug;
            # fall back to anonymous (most conservative).
            logger.warning(
                "tools/list: failed to resolve bearer to role (%s); "
                "treating as anonymous.",
                e,
            )

    # Route through the shared Registry[T] — visibility is encoded
    # in each entry's policy callable (which itself reads
    # `tools/access.py`). The dispatch from `tool_schemas` is kept
    # only for legacy import surface; the source of truth for what
    # the wire sees is the registry.
    visible_entries = tool_registry.list_visible(role)

    mcp_tool_list: List[mcp_types.Tool] = []
    for entry in visible_entries:
        try:
            tool_instance = mcp_types.Tool(
                name=entry.name,
                description=entry.meta.description,
                inputSchema=entry.meta.input_schema,
            )
            mcp_tool_list.append(tool_instance)
        except Exception as e:
            logger.error(
                f"Failed to create mcp_types.Tool instance for "
                f"'{entry.name}': {e}",
                exc_info=True,
            )

    logger.debug(
        "tools/list returned %d / %d tools for role=%s",
        len(mcp_tool_list),
        len(tool_schemas),
        role,
    )
    return mcp_tool_list


import contextvars as _cv

# Q6e: Authorization: Bearer header fallback. A Starlette middleware
# (registered in main_app.py) captures the bearer from the HTTP
# request into this contextvar; dispatch_tool_call reads from it
# when `arguments.token` is missing. Lets HTTP MCP clients
# (e.g. Claude Code with `claude mcp add --header`) authenticate via
# standard bearer auth without the router needing to byte-rewrite
# the JSON-RPC body.
#
# Default None means "no header was sent on this request"; an empty
# string would also be treated as missing.
request_auth_token: _cv.ContextVar = _cv.ContextVar(
    "request_auth_token", default=None
)


# Phase 2 Wave 2a (v5.0.63): operator-session ContextVar.
#
# The new ``@requires_role("manager")`` / ``@requires_role("operator")``
# decorators need to distinguish three caller populations that all
# arrive at ``dispatch_tool_call`` via different paths:
#
#   1. A logged-in human operator hitting the dashboard. Their request
#      flows through the FastAPI REST seam, ``require_operator_session``
#      validates the session cookie, and ``_dispatch_through_tool`` sets
#      this ContextVar to ``True`` before dispatch. The tool's bearer
#      stays the system token (so the legacy admin-only impls keep
#      working unchanged); the ContextVar lets the decorator say
#      "this call originated from a logged-in operator" without
#      conflating it with a script holding the raw system token.
#   2. A spawned agent calling MCP directly with its agent bearer.
#      The ContextVar is unset; the decorator falls through to
#      ``verify_token`` / ``get_agent_id`` against ``agents.agent_role``.
#   3. A legacy admin script using the system token in
#      ``Authorization: Bearer <token>``. Both ContextVars are set
#      (the bearer ContextVar holds the system token; the operator
#      ContextVar stays False); the decorator admits because the
#      system token satisfies any role gate.
#
# Default ``False`` matches "no operator session" — the safe default
# for any code path that bypasses the REST seam (MCP-protocol callers,
# direct dispatch_tool_call invocations from tests, in-process bridges).
operator_session_active: _cv.ContextVar = _cv.ContextVar(
    "operator_session_active", default=False
)


# Phase 3 Wave 2 (v5.0.69): operator-identity ContextVars.
#
# When the REST seam dispatches a tool call on behalf of a logged-in
# operator, it stamps the operator's user_id + the project name the
# call is targeting on these ContextVars. The Wave-2 @requires_role
# extension consults ``resolve_user_project_role(user_id, project)``
# to enforce the operator-vs-viewer split at the tool-dispatch
# boundary as well as at the router middleware. The two gates work
# in tandem:
#
#   * Router middleware (``require_operator_session_middleware``)
#     is the early gate — it 403s viewer mutations BEFORE the
#     request reaches the per-project backend at all.
#   * Decorator (``@requires_role("operator")`` etc.) is the
#     defence-in-depth gate — if a hypothetical code path
#     synthesised an in-process tool call without going through
#     the REST seam (tests, batch jobs), the resolver still rejects
#     a viewer attempt.
#
# Both ContextVars default to None so the legacy MCP-protocol path
# (agent bearer; no operator) sees "no operator identity", which
# means the project-role check is skipped and the static role gate
# (system bearer / agent_role) is the only one that runs — i.e. the
# pre-Phase-3 behaviour is preserved 1:1 for agent traffic.
operator_user_id: _cv.ContextVar = _cv.ContextVar(
    "operator_user_id", default=None
)
operator_project_name: _cv.ContextVar = _cv.ContextVar(
    "operator_project_name", default=None
)


class ToolInputValidationError(Exception):
    """Raised when caller-supplied arguments fail jsonschema validation
    after the dispatcher's pre-validation cleanup.

    Mirrors the framework's behavior (mcp.server.lowlevel.server's
    `call_tool` decorator) for callers: the message is prefixed
    "Input validation error: …" and the wrapper converts the
    exception into a CallToolResult with isError=True.
    """


def _derive_principal_from_contextvars() -> Optional[_Principal]:
    """Bridge fallback: synthesize a Principal from existing ContextVars.

    Wave 6 PR 0 — when the dispatcher is invoked without an explicit
    ``principal=`` (any old-style call site that pre-dates Wave 6),
    reconstruct one from the legacy ContextVars
    (``operator_session_active``, ``operator_user_id``,
    ``operator_project_name``, ``request_auth_token``) so old-style
    tool impls keep seeing the same identity they would have seen
    before this PR. PR 6 deletes this helper alongside the
    ContextVars themselves.

    Resolution order — bearer wins over operator_session when both
    are stamped (the harness path: AdminClient.call sets the admin
    bearer on ``request_auth_token`` while ``mcp_session``'s
    top-level setup also stamps ``operator_session_active=True``).
    Bearer is the more specific identity (it identifies a particular
    agent row, not just "some operator"), and audit-log attribution
    needs that specificity. In production code paths the two never
    both get stamped — the REST seam stamps op_session only (no
    bearer post-Wave-1 of retire-system-token); the MCP wire stamps
    bearer only.

    1. If ``request_auth_token`` resolves to an active agent
       row → agent_bearer Principal sourcing ``agent_role`` from
       the in-memory cache.
    2. Else if ``operator_session_active`` is set, the caller
       arrived via the REST seam as a logged-in operator →
       operator_session Principal naming ``operator_user_id``.
    3. Else None — the caller is anonymous; let the per-tool
       decorator's existing checks reject if appropriate.
    """
    try:
        bearer = request_auth_token.get()
    except LookupError:
        bearer = None
    if bearer:
        try:
            from ..core.auth import get_agent_id as _get_agent_id
            from ..core import globals as _g

            agent_id = _get_agent_id(bearer)
            if agent_id:
                row = _g.active_agents.get(bearer) or {}
                agent_role = row.get("agent_role")
                normalized_role = (
                    agent_role
                    if agent_role in ("worker", "manager")
                    else None
                )
                return _Principal(
                    kind="agent_bearer",
                    user_id=None,
                    agent_id=agent_id,
                    sysadmin=False,
                    project_name=None,
                    project_role=None,
                    agent_role=normalized_role,
                    can_wake_loop=False,
                    source_token=bearer,
                )
        except Exception:  # pragma: no cover - defensive
            return None

    try:
        op_session = bool(operator_session_active.get())
    except LookupError:
        op_session = False
    if op_session:
        try:
            uid = operator_user_id.get()
        except LookupError:
            uid = None
        try:
            project = operator_project_name.get()
        except LookupError:
            project = None
        return _Principal(
            kind="operator_session",
            user_id=str(uid) if uid is not None else None,
            agent_id=None,
            sysadmin=False,
            project_name=str(project) if project is not None else None,
            project_role=None,
            agent_role=None,
            can_wake_loop=False,
            source_token=None,
        )

    return None


def _tool_accepts_principal(func: Callable) -> bool:
    """Return True iff ``func`` declares a ``principal`` kwarg.

    Wave 6 PR 0 bridge — distinguishes migrated (PRs 1-5) tools
    from unmigrated ones. Inspected once per call (cheap on the
    hot path; tool implementations are stable for a process'
    lifetime). PR 6 deletes this helper alongside the bridge.
    """
    try:
        sig = _inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False
    return "principal" in sig.parameters


def _wrap_legacy_result_as_ok(
    result: Any,
) -> _ToolResult:
    """Bridge: wrap an old-style ``list[TextContent]`` return as ``Ok``.

    Wave 6 PR 0 — unmigrated tools still return the legacy shape;
    the dispatcher wraps the concatenated text into an
    ``Ok(message=...)`` so a single downstream consumer (the REST
    adapter, MCP renderer) reads a uniform ``ToolResult``.

    Already-new-style returns (``ToolResult`` variants) pass
    through unchanged.
    """
    if isinstance(result, (_Ok,)):
        return result
    # Use a runtime isinstance against the variant tuple via the
    # render module so we don't reach across to every variant
    # symbol here. Importing each by name keeps the check explicit.
    from ..core.tool_result import (
        NotFound as _NotFound,
        PermissionDenied as _PermissionDenied,
        Invalid as _Invalid,
        Conflict as _Conflict,
        Failed as _Failed,
    )
    if isinstance(result, (_NotFound, _PermissionDenied, _Invalid, _Conflict, _Failed)):
        return result
    # Legacy ``list[TextContent]`` shape — concatenate text and
    # return as Ok(message=...). Empty list → Ok with no message.
    if isinstance(result, list):
        parts: List[str] = []
        for block in result:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        message = "\n".join(parts) if parts else None
        return _Ok(message=message)
    # Anything else is a tool author bug; surface as Failed so the
    # error reaches a human eventually rather than being silently
    # rendered as a string blob.
    return _Failed(
        message=(
            f"tool returned unexpected type {type(result).__name__}; "
            f"expected list[TextContent] or ToolResult"
        )
    )


async def dispatch_tool_call(
    tool_name: str,
    raw_arguments: Union[Dict[str, Any], List[Dict[str, Any]]], # Original accepted list or dict
    *,
    principal: Optional[_Principal] = None,
) -> _ToolResult:
    """
    Handles a tool call by dispatching to the appropriate implementation.
    This replaces the logic from `@app.call_tool()` in main.py (lines 1861-1931).

    Wave 6 PR 0 — return type is now :data:`ToolResult` (the typed
    sum-type at ``agent_mcp.core.tool_result``). Two consumers:

      * REST adapter (``app/routes._dispatch_through_tool``) ``match``-es
        on the variant and maps to an HTTP status code.
      * MCP wire renderer (``app/main_app.mcp_call_tool_handler``)
        calls :func:`agent_mcp.core.tool_result.render_as_text_content`
        to convert back to the legacy ``list[TextContent]`` shape MCP
        clients consume.

    The dispatcher uses the **bridge** during the Wave 6 migration:

      * If ``principal`` is None, derive one from the legacy
        ContextVars so old-style call sites that haven't been
        updated keep working (PRs 1-5 sweep them).
      * If the tool implementation declares a ``principal`` kwarg,
        pass the Principal through; otherwise call the legacy
        signature ``func(arguments)``.
      * If the tool returns a ``list[TextContent]`` (unmigrated),
        wrap it as ``Ok(message=concatenated_text)``.
      * If the tool returns a :data:`ToolResult` variant, pass it
        through unchanged.

    PR 6 deletes the bridge: ``principal`` becomes required, and
    every tool returns :data:`ToolResult` directly.
    """
    # Sanitize arguments input (main.py:1863-1877)
    sanitized_arguments: Any
    try:
        if isinstance(raw_arguments, list):
            # The original code had a recursive call to handle_tool for lists.
            # This is complex. A simpler approach is to define if tools accept lists of args
            # or if the client should make individual calls.
            # For 1-to-1 with original `handle_tool`'s list processing:
            # This implies that a single tool call message could contain a list of argument sets
            # for the *same* tool, and the server processes them sequentially, concatenating results.
            # This is an unusual pattern for tool calls.
            # Let's assume for now that a tool call is for one set of arguments.
            # If the list processing is essential, it needs careful thought on how it interacts
            # with individual tool function signatures.
            # The original code:
            # if isinstance(arguments, list):
            #     sanitized_args = []
            #     for arg in arguments:
            #         sanitized_args.append(sanitize_json_input(arg))
            #     results = []
            #     for arg in sanitized_args:
            #         res = await handle_tool(name, arg) # Recursive call
            #         results.extend(res)
            #     return results
            # This recursive structure is problematic for a clean dispatch.
            # For now, we will assume `raw_arguments` is a single dictionary for one tool call.
            # If list processing for a single tool name is needed, the tool implementation itself
            # should be designed to handle a list of argument sets.
            # The MCP protocol itself (mcp.types) might clarify if a "tool_call" message
            # can have a list of argument sets.
            # Given the structure of `call_mcp_tool` in the prompt (singular arguments),
            # it's more likely `raw_arguments` is a single Dict.
            if isinstance(raw_arguments, dict):
                sanitized_arguments = sanitize_json_input(raw_arguments)
            else: # If it's a list, and we are not supporting recursive calls here.
                logger.error(f"Received a list of arguments for tool '{tool_name}', but registry expects a single argument dictionary per call.")
                return [mcp_types.TextContent(type="text", text="Error: Server tool dispatcher expects a single argument set, not a list.")]

        elif not isinstance(raw_arguments, dict):
            # Try to sanitize and parse if not a dict (e.g., a JSON string from a raw request)
            sanitized_arguments = sanitize_json_input(raw_arguments)
            if not isinstance(sanitized_arguments, dict):
                # If after sanitization it's still not a dict, it's an invalid format.
                raise ValueError(f"Tool arguments for '{tool_name}' must be a dictionary after sanitization, got {type(sanitized_arguments)}")
        else: # It's already a dict
            sanitized_arguments = sanitize_json_input(raw_arguments) # Still sanitize it

    except ValueError as e:
        logger.error(f"Invalid input arguments for tool '{tool_name}': {e}")
        from ..core.tool_result import Invalid as _Invalid
        return _Invalid(field=None, message=f"Invalid input arguments: {str(e)}")
    except Exception as e: # Catch any other sanitization errors
        logger.error(f"Error sanitizing arguments for tool '{tool_name}': {e}", exc_info=True)
        from ..core.tool_result import Failed as _Failed
        return _Failed(message=f"Error processing tool arguments: {str(e)}")


    # Dispatch to the correct tool implementation (main.py:1879 onwards)
    if tool_name in tool_implementations:
        implementation_func = tool_implementations[tool_name]
        try:
            # The core logic of each tool (e.g., create_agent_tool_impl)
            # will now handle its own argument extraction (e.g., .get("token"))
            # and authentication/authorization if necessary.
            # This dispatch_tool_call function focuses on routing.
            # We pass the sanitized_arguments dictionary directly.
            # The original handle_tool had specific .get calls for each tool.
            # This will now be the responsibility of the individual tool_impl functions.
            # For example, create_agent_tool_impl(arguments: Dict) -> ...
            # inside create_agent_tool_impl: token = arguments.get("token"), agent_id = arguments.get("agent_id")
            
            # This is a key design decision:
            # Option A: Dispatcher unpacks args: `return await func(sanitized_args.get("token"), ...)` (like original)
            # Option B: Dispatcher passes dict: `return await func(sanitized_arguments)` (current choice)
            # Option B is more flexible if tool signatures vary widely or use **kwargs.
            # It makes individual tool functions responsible for their arg parsing.
            
            # For closer 1-to-1 with original's direct arg passing, we'd need a huge if/elif here.
            # Let's stick to Option B for better modularity, assuming tool_impl functions
            # are adapted to take a single dictionary of arguments.
            # If a strict 1-to-1 call signature is needed for each specific tool as in the original main.py,
            # then the `tool_implementations` would need to store lambdas or wrappers.
            # e.g. `lambda args: create_agent_tool_impl(args.get("token"), args.get("agent_id"), ...)`
            # This becomes cumbersome.
            # The most straightforward refactor is that tool_impl functions now take `(arguments: Dict[str, Any])`.

            # The original `handle_tool` in main.py (lines 1880-1931) had a large if/elif block.
            # This is now replaced by the `tool_implementations` dictionary lookup.
            # Each specific tool's logic (argument extraction, calling the core function)
            # will be in its own `*_tool.py` file, which registers its implementation.
            # The implementation function itself will handle argument extraction.
            
            # Example: if tool_name == "create_agent":
            #   return await create_agent_tool_impl(sanitized_arguments)
            # This is handled by the dict lookup now.

            # -32602 regression fix: clean arguments before schema
            # validation so real client shapes don't get rejected:
            #
            # - `token: null` from LLMs serializing the optional field
            # - `_meta` leaked from params into arguments by some SDKs
            # - integer-as-string for properties declared `"type":
            #   "integer"`
            #
            # See `_clean_arguments_for_schema` for the full
            # rationale; tests in
            # `tests/test_call_tool_argument_tolerance.py` pin the
            # invariant against the same handler real MCP clients hit.
            input_schema = _find_schema_for(tool_name)
            sanitized_arguments = _clean_arguments_for_schema(
                sanitized_arguments, input_schema
            )

            # Run schema validation ourselves (the @call_tool decorator
            # registers us with `validate_input=False` so it doesn't
            # validate the *uncleaned* arguments first). Match the
            # framework's error wording so clients with bespoke text
            # matching keep working. Raising (instead of returning text)
            # lets the framework's wrapper set isError=True via
            # `_make_error_result`, same as it would have done if
            # validation had run upstream of us.
            if _jsonschema is not None and input_schema:
                try:
                    _jsonschema.validate(
                        instance=sanitized_arguments, schema=input_schema
                    )
                except _jsonschema.ValidationError as e:
                    raise ToolInputValidationError(
                        f"Input validation error: {e.message}"
                    )

            # Q6e: inject token from the Authorization-header contextvar
            # when the caller didn't put one in arguments (or sent an
            # empty / null one — `_clean_arguments_for_schema` strips
            # nulls; we still guard against ""). Explicit non-empty
            # arguments.token always wins (no silent override).
            if not sanitized_arguments.get("token"):
                header_token = request_auth_token.get()
                if header_token:
                    sanitized_arguments = {**sanitized_arguments, "token": header_token}

            # Wave 6 PR 0 bridge — derive Principal from ContextVars
            # when the caller didn't pass one (every pre-Wave-6 call
            # site). PR 6 makes the kwarg required and deletes this
            # fallback.
            effective_principal = principal
            if effective_principal is None:
                effective_principal = _derive_principal_from_contextvars()

            # Bridge: thread Principal through only when the tool
            # impl declares it. Migrated (PRs 1-5) tools take it as a
            # keyword-only arg; legacy tools don't, and would
            # ``TypeError`` if we always passed it.
            if _tool_accepts_principal(implementation_func):
                raw_result = await implementation_func(
                    sanitized_arguments, principal=effective_principal
                )
            else:
                raw_result = await implementation_func(sanitized_arguments)

            # Issue H is now handled by the @requires / @requires_policy
            # decorators in agent_mcp/core/authorize.py: they raise
            # AuthRejected directly, which the `except AuthRejected`
            # arm below catches. The legacy text-matching shim
            # (_AUTH_FAILURE_RE / ToolAuthError / _raise_if_auth_failure)
            # was deleted in the consolidation cleanup commit; if any
            # future tool re-introduces a hand-rolled "Unauthorized:"
            # text response, it will silently regress to isError=False
            # and tests/test_auth_decorators.py will catch the
            # _AUTH_FAILURE_RE re-introduction.

            # Bridge: wrap legacy ``list[TextContent]`` returns as
            # ``Ok(message=...)``; pass ``ToolResult`` variants
            # through unchanged. PR 6 deletes this — every tool
            # will return ``ToolResult`` directly.
            return _wrap_legacy_result_as_ok(raw_result)

        except AuthRejected as e:
            # Decorator-raised auth failure (architecture review
            # 2026-06-01 candidate A). Re-raise so the MCP framework's
            # `_make_error_result` (`mcp/server/lowlevel/server.py:584`)
            # turns it into a `CallToolResult` with `isError=True` and
            # `text="Unauthorized: <reason>"`. We don't catch + return
            # here because the framework already does the right thing
            # with exceptions; this stanza exists for the type to be
            # visible at the dispatch boundary and to give us a clean
            # hook if we ever need to log/audit auth failures
            # uniformly.
            logger.info(
                f"Tool '{tool_name}' auth-rejected: {e.reason}"
            )
            raise

        except ToolInputValidationError as e:
            # Caller-error path; logged at info level (not a server
            # bug) but still raised so isError=True reaches the
            # client (mcp/server/lowlevel/server.py:541 → 542).
            logger.info(f"Tool '{tool_name}' rejected arguments: {e}")
            raise
        except Exception as e:
            logger.error(f"Error executing tool '{tool_name}': {e}", exc_info=True)
            # Re-raise so the MCP framework's `_make_error_result`
            # sets isError=True. Previously we swallowed and returned
            # text, which kept isError=False for any exception.
            raise
    else:
        logger.warning(f"Unknown tool called: {tool_name}")
        # Original main.py:1930 (raise ValueError(f"Unknown tool: {name}"))
        # Returning an error message is friendlier for an API.
        from ..core.tool_result import NotFound as _NotFound
        return _NotFound(resource="tool", identifier=tool_name)

# The actual tool schemas and implementations will be populated by calls to `register_tool`
# from each of the specific tool modules (e.g., admin_tools.py, task_tools.py, etc.)
# when those modules are imported by the application (e.g., in mcp_server_src/tools/__init__.py).