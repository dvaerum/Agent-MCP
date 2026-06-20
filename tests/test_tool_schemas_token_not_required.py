"""Tool inputSchemas must NOT mark `token` as required (issue J).

Two prior agents (claude-code sessions) independently reported:

    `get_agent_messages` still requires a token parameter that isn't in
    the schema. This looks like a server-side bug — the tool advertises
    optional params only but validates for a required token that
    clients can't provide.

    Same 'token' is a required property error after reconnect — this is
    a schema/validator mismatch, not a session issue.

Root cause: the router's `tools/list` rewrite strips `token` from the
inputSchema shown to the model, but the MCP framework's `tools/call`
handler validates `arguments` against the *original* (server-side)
inputSchema BEFORE the dispatcher (and the Q6e ContextVar fallback)
gets a chance to inject the token. Because every tool's schema still
listed `token` in its `required` array, bearer-auth-only callers were
rejected upstream of the fallback.

Fix (Option B from the prior analysis / plan Q6e follow-up): drop
`token` from `required` in every tool's inputSchema while keeping it
as an optional property. The Q6e middleware then fills it in from
the Authorization: Bearer header when the caller omitted it.

These tests pin the invariant and exercise the full call_tool path
(framework schema validation + dispatch + auth) against a couple of
representative tools.
"""

from __future__ import annotations

import asyncio

import mcp.types as mcp_types
import pytest


# --- Invariant: no tool's inputSchema may list "token" in `required` ---

def test_no_tool_lists_token_in_required() -> None:
    """Walk every registered tool's inputSchema and assert `token` is
    not in `required`. This is the regression guard for issue J: any
    new tool that lists `token` as required would re-introduce the
    "token is a required property" rejection for bearer-auth callers.
    """
    # Importing the package triggers tool registration.
    import agent_mcp.tools  # noqa: F401
    from agent_mcp.tools.registry import tool_schemas

    assert tool_schemas, "tool registry is empty — registration didn't run"

    offenders: list[str] = []
    for entry in tool_schemas:
        schema = entry.get("inputSchema") or {}
        required = schema.get("required") or []
        if "token" in required:
            offenders.append(entry["name"])

    assert not offenders, (
        "These tools still list `token` in inputSchema.required, which "
        "rejects bearer-auth-only MCP clients (issue J): "
        f"{sorted(offenders)}"
    )


def test_token_still_present_as_optional_property() -> None:
    """Removing `token` from `required` must keep it as a valid
    optional property in the schema so the Q6e dispatcher fallback
    (and explicit-token callers) keep working.

    We only check tools that actually consume `token` from arguments
    (i.e. they used to require it).
    """
    import agent_mcp.tools  # noqa: F401
    from agent_mcp.tools.registry import tool_schemas

    # Tools that historically required `token` per a grep of the
    # codebase prior to this PR.
    tools_with_token = {
        "send_agent_message",
        "get_agent_messages",
        "broadcast_admin_message",
        "view_project_context",
        "update_project_context",
        "bulk_update_project_context",
        "search_project_context",
        "delete_project_context",
        "view_tasks",
        "view_status",
        "assign_task",
        "create_self_task",
        "update_task_status",
        "request_assistance",
        "view_file_metadata",
        "update_file_metadata",
        "check_file_status",
        "update_file_status",
        "ask_project_rag",
        "get_system_prompt",
        "create_agent",
        "terminate_agent",
        "relaunch_agent",
        "restore_agent",
        "purge_agent",
    }

    by_name = {e["name"]: e for e in tool_schemas}
    for tn in tools_with_token & by_name.keys():
        schema = by_name[tn].get("inputSchema") or {}
        props = schema.get("properties") or {}
        assert "token" in props, (
            f"{tn}: `token` should remain a valid optional property "
            "(the Q6e bearer-header fallback still injects it)"
        )


# --- End-to-end: framework validates arguments against inputSchema ---
# The MCP framework's lowlevel server (mcp/server/lowlevel/server.py
# `call_tool` decorator) runs `jsonschema.validate(arguments, tool.inputSchema)`
# BEFORE invoking the registered dispatcher. If `token` is required and
# arguments={}, validation fails with `Input validation error: 'token' is
# a required property` and the dispatcher (incl. the Q6e ContextVar
# token-injection) never runs.
#
# These tests invoke the framework's CallToolRequest handler directly
# with empty `arguments` and the Q6e ContextVar set, mimicking what
# happens when a real MCP client passes `Authorization: Bearer <admin>`
# with no `token` in the JSON-RPC body.


def _admin_token(client) -> str:
    # Wave 1 of prancy-napping-pie put `/api/tokens` behind
    # `require_operator_session`. The lifespan-populated value lives on
    # `agent_mcp.core.globals.admin_token`; read it directly to keep the
    # test independent of the dep's auth fallback chain.
    from agent_mcp.core import globals as g
    return g.admin_token


async def _call_tool_via_framework(tool_name: str, arguments: dict):
    """Invoke the registered MCP CallToolRequest handler directly,
    which runs the framework's schema validation step exactly as it
    would for a real SSE/JSON-RPC client."""
    from agent_mcp.app.main_app import mcp_app_instance

    handler = mcp_app_instance.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(
            name=tool_name, arguments=arguments
        ),
    )
    return await handler(req)


def _result_text(server_result) -> str:
    """Pull the human-readable text out of a CallToolResult."""
    # ServerResult wraps a CallToolResult; CallToolResult.content is
    # a list of TextContent/etc.
    inner = server_result.root if hasattr(server_result, "root") else server_result
    blocks = getattr(inner, "content", None) or []
    parts = [getattr(b, "text", "") for b in blocks]
    return "\n".join(p for p in parts if p)


def _is_input_validation_error(server_result) -> bool:
    inner = server_result.root if hasattr(server_result, "root") else server_result
    if not getattr(inner, "isError", False):
        return False
    return "Input validation error" in _result_text(server_result)


@pytest.mark.parametrize(
    "tool_name,extra_args",
    [
        # Three tools the user explicitly hit in production.
        ("get_agent_messages", {}),
        ("view_project_context", {}),
        ("view_tasks", {}),
    ],
)
def test_framework_does_not_reject_call_without_arguments_token(
    client, tool_name: str, extra_args: dict
) -> None:
    """With the Authorization: Bearer ContextVar set and NO `token` in
    `arguments`, the framework must NOT reject the call with
    `'token' is a required property`. Pre-fix this fails; post-fix the
    dispatcher's Q6e fallback fills in the token from the ContextVar
    and the call reaches the tool.
    """
    from agent_mcp.tools.registry import request_auth_token

    admin = _admin_token(client)
    request_auth_token.set(admin)

    result = asyncio.run(
        _call_tool_via_framework(tool_name, dict(extra_args))
    )

    text = _result_text(result)
    assert not _is_input_validation_error(result), (
        f"{tool_name}: framework rejected the call with schema validation "
        f"before the Q6e fallback could inject the token: {text}"
    )
    # The call may still produce an error for unrelated reasons in the
    # test harness (e.g. the admin token isn't an agent so
    # get_agent_messages may return an empty list or a friendly error),
    # but it must not be an "Unauthorized" rejection either — the
    # admin token was injected via the ContextVar.
    assert "Unauthorized" not in text and "Invalid" not in text, (
        f"{tool_name}: token from Authorization header was not picked up "
        f"by the dispatcher fallback: {text}"
    )
