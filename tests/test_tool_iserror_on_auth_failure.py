"""Tool calls that fail on auth must surface as isError, not silent success.

UPSTREAM_ISSUES.md issue H: today, tools like view_status reject a
garbage token by returning `TextContent(text="Unauthorized: Admin
token required")` with `isError=False`. Naive clients keying off
isError treat the failure as success.

Pre-Wave-6 fix: when a tool returned auth-failure text,
`dispatch_tool_call` raised an exception (the framework's
`_make_error_result` then set isError=True). With Wave 6 PR 5's
migration of admin tools to :class:`ToolResult`, the typed
:class:`PermissionDenied` variant carries that signal at the type
level — no text-matching, no raise. The REST adapter maps it to
403; the MCP wire renderer turns it into ``"Unauthorized: ..."``
text which the harness's ``assert_unauthorized`` helper / dashboard
client both recognise as a denied call.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_dispatch_tool_call_returns_permission_denied_on_bad_token() -> None:
    """A tool that's gated by an operator-only check returns
    :class:`PermissionDenied` when the caller has no operator
    principal — even if a (wrong) token is supplied in arguments.

    Wave 6 PR 5: ``view_status`` is migrated to take a
    :class:`Principal` and return a :class:`ToolResult`. The bridge
    in ``dispatch_tool_call`` derives a Principal from the
    legacy ContextVars; outside ``mcp_session``, no contextvar is
    set, so the derived principal is None and the inline
    operator check returns :class:`PermissionDenied`.
    """
    from agent_mcp.core.tool_result import PermissionDenied
    from agent_mcp.tools.registry import dispatch_tool_call

    result = await dispatch_tool_call(
        "view_status",
        {"token": "deadbeef" * 4},
    )
    assert isinstance(result, PermissionDenied), (
        f"expected PermissionDenied for unauthenticated view_status; "
        f"got {result!r}"
    )


@pytest.mark.asyncio
async def test_dispatch_tool_call_passes_through_normal_success() -> None:
    """A tool returning regular content must still pass through (no false positives).

    Wave 6 PR 0 — ``dispatch_tool_call`` now returns
    :data:`agent_mcp.core.tool_result.ToolResult`; the bridge
    wraps an unmigrated tool's ``list[TextContent]`` return as
    ``Ok(message=...)``. The successful-tool path therefore
    surfaces as ``Ok``, not a list.
    """
    from agent_mcp.core.tool_result import Ok
    from agent_mcp.tools.registry import dispatch_tool_call

    # The simplest "always works" tool: `test` (no auth required per upstream).
    # If it doesn't exist on this build, fall back to no-op.
    try:
        result = await dispatch_tool_call("test", {})
    except KeyError:
        pytest.skip("no `test` tool registered on this build")
    # Just assert no exception + result is a success ToolResult.
    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
