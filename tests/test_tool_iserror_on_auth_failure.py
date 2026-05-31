"""Tool calls that fail on auth must surface as isError, not silent success.

UPSTREAM_ISSUES.md issue H: today, tools like view_status reject a
garbage token by returning `TextContent(text="Unauthorized: Admin
token required")` with `isError=False`. Naive clients keying off
isError treat the failure as success.

Fix: when a tool returns a text content whose body matches an
auth-failure pattern (Unauthorized/Invalid token/Admin token
required), `dispatch_tool_call` raises an exception. The MCP
framework catches and wraps it into a CallToolResult with
isError=True (per mcp/server/lowlevel/server.py `_make_error_result`).
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_dispatch_tool_call_raises_on_unauthorized_response() -> None:
    """A tool that returns Unauthorized text must cause dispatch_tool_call
    to raise — the framework then sets isError=True automatically."""
    from agent_mcp.tools.registry import dispatch_tool_call

    # view_status is admin-only; with a garbage token it returns the
    # "Unauthorized" text payload. Pre-fix: returns text + isError stays
    # false. Post-fix: raises.
    with pytest.raises(Exception):
        await dispatch_tool_call(
            "view_status",
            {"token": "deadbeef" * 4},
        )


@pytest.mark.asyncio
async def test_dispatch_tool_call_passes_through_normal_success() -> None:
    """A tool returning regular content must still pass through (no false positives)."""
    from agent_mcp.tools.registry import dispatch_tool_call

    # The simplest "always works" tool: `test` (no auth required per upstream).
    # If it doesn't exist on this build, fall back to no-op.
    try:
        result = await dispatch_tool_call("test", {})
    except KeyError:
        pytest.skip("no `test` tool registered on this build")
    # Just assert no exception + result is list of content.
    assert isinstance(result, list)
    assert all(hasattr(c, "text") for c in result)
