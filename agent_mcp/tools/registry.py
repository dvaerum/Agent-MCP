# Agent-MCP/mcp_template/mcp_server_src/tools/registry.py
from typing import List, Dict, Any, Callable, Awaitable, Optional, Union
import mcp.types as mcp_types # Assuming this is the correct import for your mcp.types

# Import utility for JSON sanitization, as handle_tool uses it
from ..utils.json_utils import sanitize_json_input, get_sanitized_json_body
# Import the central logger
from ..core.config import logger
# Typed auth-failure exception from the decorator surface; the
# dispatcher catches it explicitly so the audit log line is uniform.
from ..core.authorize import AuthRejected

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


# --- Core Tool Registry Functions ---

def register_tool(
    name: str,
    description: str,
    input_schema: Dict[str, Any],
    implementation: Callable[..., Awaitable[List[mcp_types.TextContent]]]
):
    """
    Registers a tool's schema and its implementation.
    This function will be called by each tool module to register itself.
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

    try:
        from .access import is_visible_to_role
    except Exception as e:
        # If the access module itself fails to import (shouldn't
        # happen in production), surface every tool — the previous
        # behavior — and log loudly.
        logger.error(
            "tools/list: access module unavailable (%s); falling back "
            "to unfiltered catalogue.",
            e,
        )

        def is_visible_to_role(_name: str, _role: str) -> bool:
            return True

    mcp_tool_list: List[mcp_types.Tool] = []
    for schema_dict in tool_schemas:
        name = schema_dict.get("name", "")
        if not is_visible_to_role(name, role):
            continue
        try:
            tool_instance = mcp_types.Tool(
                name=schema_dict["name"],
                description=schema_dict["description"],
                inputSchema=schema_dict["inputSchema"]
            )
            mcp_tool_list.append(tool_instance)
        except Exception as e:
            logger.error(f"Failed to create mcp_types.Tool instance for '{schema_dict.get('name', 'Unknown')}': {e}", exc_info=True)
            # Optionally, skip this tool or add a placeholder error tool.
            # For now, skipping problematic ones.

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
    raw_arguments: Union[Dict[str, Any], List[Dict[str, Any]]] # Original accepted list or dict
) -> List[mcp_types.TextContent]:
    """
    Handles a tool call by dispatching to the appropriate implementation.
    This replaces the logic from `@app.call_tool()` in main.py (lines 1861-1931).
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
        return [mcp_types.TextContent(type="text", text=f"Invalid input arguments: {str(e)}")]
    except Exception as e: # Catch any other sanitization errors
        logger.error(f"Error sanitizing arguments for tool '{tool_name}': {e}", exc_info=True)
        return [mcp_types.TextContent(type="text", text=f"Error processing tool arguments: {str(e)}")]


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

            result = await implementation_func(sanitized_arguments)

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
        # Returning an error message is friendlier for an API.
        return [mcp_types.TextContent(type="text", text=f"Error: Unknown tool '{tool_name}'.")]

# The actual tool schemas and implementations will be populated by calls to `register_tool`
# from each of the specific tool modules (e.g., admin_tools.py, task_tools.py, etc.)
# when those modules are imported by the application (e.g., in mcp_server_src/tools/__init__.py).