# Agent-MCP/mcp_template/mcp_server_src/tools/registry.py
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
# Wave 6 — Principal + ToolResult are the canonical dispatch
# vocabulary. The bridge that allowed unmigrated tools (legacy
# ``list[TextContent]`` returns, no ``principal`` kwarg) to coexist
# with the new contract was removed in PR 6 — every tool now takes
# ``principal`` and returns :data:`ToolResult` directly.
from ..core.principal import Principal as _Principal
from ..core.tool_result import (
    Ok as _Ok,
    NotFound as _NotFound,
    PermissionDenied as _PermissionDenied,
    Invalid as _Invalid,
    Conflict as _Conflict,
    Failed as _Failed,
    ToolResult as _ToolResult,
)

# Concrete variant tuple for isinstance() — :data:`ToolResult` is a
# Union alias, which can't be used as the second arg to isinstance().
_TOOL_RESULT_VARIANTS = (_Ok, _NotFound, _PermissionDenied, _Invalid, _Conflict, _Failed)

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
# The legacy self-auth ``token`` param is retired (token-retirement
# plan Phase C): identity comes from the header-Bearer Principal, not
# from an arg. It's no longer in any tool schema, so
# ``additionalProperties: false`` would 400 a stray ``token`` sent by
# an old client. Drop it here instead — tolerate-and-ignore keeps the
# transition graceful. (``agent_token``/``agent_id``/``recipient_id``
# target ANOTHER agent and are NOT in this set.)
_RESERVED_ARG_KEYS = frozenset({"_meta", "meta", "token"})


def _clean_arguments_for_schema(
    arguments: Dict[str, Any], schema: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Make caller-supplied arguments tolerant of the real-world shapes
    that LLM-driven MCP clients regularly produce and that
    `jsonschema.validate(arguments, inputSchema)` would otherwise
    reject:

    1.  `{"token": ..., ...}` — the legacy self-auth `token` param is
        retired (identity now comes from the header-Bearer Principal),
        so it's gone from every schema. `additionalProperties: false`
        would 400 a stray `token` an old client still sends. `token`
        is a reserved key here → dropped silently (tolerate-and-ignore
        during the transition).

    2.  `{"_meta": {...}, ...}` — some MCP client SDKs leak the
        spec-defined `_meta` field (which belongs at the params
        level) into the arguments object. Schemas use
        `additionalProperties: false`, which rejects it. Drop the
        reserved-name keys before validation.

    3.  `{"anything": null, ...}` — models serialize an optional field
        as JSON null when they decide "I don't have one to send".
        Schemas declare typed properties (no `null` accepted), so
        validation would fail with `'None is not of type ...'`. Strip
        any top-level key whose value is `None`; the tool's own arg
        parsing treats absent and null the same.

    Schema-driven cleanups go on top:

    4.  If the property is `{"type": "integer"}` and the value is a
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
    table from registry introspection (the impl's
    ``_required_capability`` cap gate, with the kwarg as an explicit
    tighten-only override) without re-parsing source.
    """

    description: str
    input_schema: Dict[str, Any]
    implementation: Callable[..., Awaitable[List[mcp_types.TextContent]]]
    declared_visibility: str = "any"


class ToolRegistry(Registry[ToolImpl]):
    """Tool subsystem adapter for the shared Registry.

    The schema + impl live in `entry.meta`; visibility is a callable
    that consults `tools.access.is_visible_to_role` so every existing
    classification (admin / any / worker-if-toggled:<key>) continues
    to govern `tools/list` filtering. Dispatch itself goes through
    module-level `dispatch_tool_call` (below), not a method on this
    class — `dispatch_tool_call` owns argument sanitization, schema
    validation, and Principal threading that a bare `entry.get(name)`
    lookup doesn't have.
    """


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

    ``visibility``: one of ``"operator"``, ``"manager"``, ``"worker"``,
    ``"any"``, or ``"worker-if-toggled:<key>[,<key>...]"``. Surfaces the
    access policy at the registration site so the registry /
    ``tools/list`` filter / UI can introspect it without re-reading
    source. Defaults to ``"any"``.

    For a tool gated by ``@requires_capability`` the visibility is
    DERIVED from the cap (arch-r3 #1+5); this kwarg then acts as a
    tighten-only override (it may hide the tool from a role the cap
    admits, never advertise it to a role the cap rejects). For a tool
    whose cap check is in-body (no ``_required_capability`` on the
    wrapper) the kwarg is the sole visibility signal.

    Enforcement of the policy lives at the call site in the impl's
    ``@requires_capability(...)`` / ``@requires_policy(...)`` decorator
    (or an in-body ``_require_capability`` check) — the kwarg here is
    metadata for the visibility filter, not the auth gate, and serves
    only as a tighten-only override of the cap-derived tier. See
    :mod:`agent_mcp.tools.access` for the derivation rationale.
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


async def list_available_tools(
    *, principal: Optional[_Principal] = None,
) -> List[mcp_types.Tool]:
    """
    Returns a list of available tools with their schemas.
    This replaces the logic from `@app.list_tools()` in main.py (lines 1636-1858).
    It now reads from the `tool_schemas` list populated by `register_tool`.

    The list is filtered by the calling principal's role (admin /
    worker / anonymous) per
    :func:`agent_mcp.tools.access.is_visible_to_role`. Callers without
    a Principal in hand (None) are treated as anonymous and see only
    "any"-classified tools.

    Wave 6 PR 6: the legacy bearer-token role-resolver branch
    (which consulted ``verify_token`` + ``get_agent_id`` against
    ``request_auth_token``) is gone; identity is now carried via the
    typed Principal threaded through from the MCP wire / REST seam.

    Phase 7g: previously this returned every registered tool to every
    caller. That worked while the router's `_rewrite_tools_list_event`
    hid admin-only tools, but Phase 7f removed that rewrite (per Q7.1:
    no MCP-protocol manipulation in the router). Without backend-side
    filtering, workers saw the full catalogue, attempted admin tools,
    and got isError=true (PR #15) — wasting tokens and confusing the
    model.
    """
    # arch-r3 #1+5 PR-B: the catalog role is derived by the single
    # ``catalog_role`` function shared with prompts/list, prompts/get,
    # and resources/list+read — so a given Principal resolves to the
    # SAME role on every MCP catalog surface. (Previously this site
    # mapped a viewer forwarding-header caller to ``"anonymous"`` while
    # the prompts surface mapped it to ``"worker"``.)
    from ..core.principal_builder import catalog_role

    role = catalog_role(principal)

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


# Wave 6 PR 6: per-request Principal ContextVar.
#
# The MCP wire path (``mcp_call_tool_handler`` in ``app/main_app.py``)
# is a bare framework callback with no Request handle — it cannot read
# ``request.state.principal``. ``AuthHeaderMiddleware`` stamps this
# ContextVar at the same time it stamps ``request.state.principal``;
# the MCP handler reads it back and threads it explicitly into
# :func:`dispatch_tool_call`.
#
# Distinct from the deleted operator-session ContextVars: this carries
# the full Principal value (the source of truth), not a denormalized
# flag-and-fields shape that decorators have to re-derive identity
# from. The dispatcher itself takes ``principal`` as a required
# keyword arg — this ContextVar is purely the seam between middleware
# and the MCP handler.
request_principal: _cv.ContextVar = _cv.ContextVar(
    "request_principal", default=None
)


class ToolInputValidationError(Exception):
    """Raised when caller-supplied arguments fail jsonschema validation
    after the dispatcher's pre-validation cleanup.

    Mirrors the framework's behavior (mcp.server.lowlevel.server's
    `call_tool` decorator) for callers: the message is prefixed
    "Input validation error: …" and the wrapper converts the
    exception into a CallToolResult with isError=True.
    """


async def dispatch_tool_call(
    tool_name: str,
    raw_arguments: Union[Dict[str, Any], List[Dict[str, Any]]], # Original accepted list or dict
    *,
    principal: Optional[_Principal] = None,
) -> _ToolResult:
    """
    Handles a tool call by dispatching to the appropriate implementation.

    Return type is :data:`ToolResult` (the typed sum-type at
    ``agent_mcp.core.tool_result``). Two consumers:

      * REST adapter (``app/routes._dispatch_through_tool``) matches
        on the variant and maps to an HTTP status code.
      * MCP wire renderer (``app/main_app.mcp_call_tool_handler``)
        calls :func:`agent_mcp.core.tool_result.render_as_text_content`
        to convert back to the legacy ``list[TextContent]`` shape MCP
        clients consume.

    Wave 6 PR 6: ``principal`` is the canonical identity carrier and
    every production seam (AuthHeaderMiddleware, ``_dispatch_through_tool``,
    test harness) passes it explicitly. The legacy bridge
    (ContextVar-derived principal + ``list[TextContent]`` auto-wrap)
    is gone. For direct in-process / unit-test callers that haven't
    threaded a Principal through, a narrow fallback synthesizes an
    ``agent_bearer`` Principal from the ``request_auth_token``
    ContextVar — same contract every per-tool decorator already uses
    for direct calls.
    """
    # Sanitize arguments input (main.py:1863-1877)
    sanitized_arguments: Any
    try:
        if isinstance(raw_arguments, list):
            # A tool call is always one argument set; the MCP protocol
            # never sends a list of argument-sets for a single call.
            # Reject cleanly rather than guessing at batch semantics.
            logger.error(f"Received a list of arguments for tool '{tool_name}', but registry expects a single argument dictionary per call.")
            return _Invalid(
                field=None,
                message=(
                    "Server tool dispatcher expects a single argument set, "
                    "not a list."
                ),
            )

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
        return _Invalid(field=None, message=f"Invalid input arguments: {str(e)}")
    except Exception as e: # Catch any other sanitization errors
        # SD-R7-1: an unexpected sanitization error is a server-side bug,
        # not caller feedback — its ``str(e)`` can carry internals. Log
        # the detail server-side; return a STATIC generic message. (The
        # ``ValueError`` arm above keeps its message: those are controlled
        # argument-format validation strings, safe caller feedback.)
        logger.error(f"Error sanitizing arguments for tool '{tool_name}': {e}", exc_info=True)
        return _Failed(message="Error processing tool arguments")


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
            # inside create_agent_tool_impl: agent_id = arguments.get("agent_id")

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

            # Direct-call fallback (tests / in-process scripts that
            # didn't thread a Principal through): synthesize one from
            # the ``request_auth_token`` ContextVar. Production seams
            # always pass an explicit ``principal=`` so this path is
            # dead weight in the deployed router.
            effective_principal = principal
            if effective_principal is None:
                from ..core.authorize import _synthesize_principal_from_arguments
                effective_principal = _synthesize_principal_from_arguments(
                    sanitized_arguments,
                )

            # Issue H is now handled by the @requires / @requires_policy
            # decorators in agent_mcp/core/authorize.py: they raise
            # AuthRejected directly, which the `except AuthRejected`
            # arm below catches.
            #
            # Pass ``principal=`` only when the tool impl actually
            # declares the kwarg — test fixtures and ad-hoc tools may
            # not. The decorator wraps the real tool and accepts the
            # kwarg even if the inner impl doesn't, so production
            # tools (which always go through a @requires* decorator)
            # always take the principal path.
            import inspect as _inspect
            try:
                sig = _inspect.signature(implementation_func)
                takes_principal = "principal" in sig.parameters or any(
                    p.kind == _inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                )
            except (TypeError, ValueError):  # pragma: no cover - defensive
                takes_principal = False
            if takes_principal:
                result = await implementation_func(
                    sanitized_arguments, principal=effective_principal,
                )
            else:
                result = await implementation_func(sanitized_arguments)
            if not isinstance(result, _TOOL_RESULT_VARIANTS):
                # Defensive — every tool impl returns a ToolResult
                # variant post-PR-6. Anything else is a tool author
                # bug; surface as Failed so the error reaches a human
                # eventually rather than crashing the request.
                return _Failed(
                    message=(
                        f"tool '{tool_name}' returned unexpected type "
                        f"{type(result).__name__}; expected ToolResult"
                    )
                )
            return result

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
        # Returning a typed NotFound lets the REST adapter map to 404.
        return _NotFound(resource="tool", identifier=tool_name)

# The actual tool schemas and implementations will be populated by calls to `register_tool`
# from each of the specific tool modules (e.g., admin_tools.py, task_tools.py, etc.)
# when those modules are imported by the application (e.g., in mcp_server_src/tools/__init__.py).