"""Round-3 finding AS-1: MCP ``isError`` fidelity on RETURNED denials.

The MCP framework (``mcp/server/lowlevel/server.py``) sets
``isError=True`` only when the registered ``call_tool`` handler
*raises*. A tool that *returns* a :class:`PermissionDenied`
``ToolResult`` used to reach the client rendered as
``"Unauthorized: ..."`` text but with ``isError=False`` — a denial
that looks like a successful call to any client keying off the flag.

Decorator-gated tools that RAISE ``AuthRejected`` correctly get
``isError=True`` (see ``test_tool_iserror_on_auth_failure.py`` and the
re-raise in ``tools/registry.dispatch_tool_call``). This suite pins the
residual gap: the RETURNED-denial path must reach the MCP client with
``isError=True`` too, so both denial paths agree.

We exercise the *real* framework handler
(``mcp_app_instance.request_handlers[CallToolRequest]``) — the same
code path a live MCP client hits — and assert on the resulting
``CallToolResult.isError``. ``dispatch_tool_call`` is monkeypatched to
return / raise a chosen shape so the test isolates the wire-flag
decision from any particular tool's policy.
"""

from __future__ import annotations

import mcp.types as mcp_types
import pytest

import agent_mcp.app.main_app as main_app
from agent_mcp.core.authorize import AuthRejected
from agent_mcp.core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
)


async def _call_through_framework(name: str = "any_tool") -> mcp_types.CallToolResult:
    """Drive a ``tools/call`` through the framework's registered handler
    and return the resulting :class:`CallToolResult` — exactly what an
    MCP client receives on the wire (``isError`` and all).
    """
    handler = main_app.mcp_app_instance.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments={}),
    )
    server_result = await handler(req)
    call_result = server_result.root
    assert isinstance(call_result, mcp_types.CallToolResult), (
        f"expected CallToolResult, got {type(call_result).__name__}"
    )
    return call_result


@pytest.mark.asyncio
async def test_returned_permission_denied_reaches_client_as_iserror(monkeypatch) -> None:
    """The AS-1 fix: a tool that RETURNS :class:`PermissionDenied`
    reaches the MCP client with ``isError=True`` — matching a RAISED
    ``AuthRejected`` denial, not masquerading as a success.
    """

    async def _fake_dispatch(name, arguments, *, principal=None):
        return PermissionDenied(reason="operator token required")

    monkeypatch.setattr(main_app, "dispatch_tool_call", _fake_dispatch)

    result = await _call_through_framework()

    assert result.isError is True, (
        "a RETURNED PermissionDenied must reach the client with "
        f"isError=True; got isError={result.isError!r}"
    )
    # The rendered denial text still surfaces so clients see the reason.
    assert any("Unauthorized" in b.text for b in result.content), (
        f"expected 'Unauthorized' text; got {[b.text for b in result.content]!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    [
        NotFound(resource="task", identifier="42"),
        Invalid(field="text", message="empty"),
        Conflict(reason="duplicate agent_id"),
        Failed(message="db write returned no row id"),
    ],
)
async def test_returned_error_variants_reach_client_as_iserror(monkeypatch, variant) -> None:
    """Every non-``Ok`` variant that a tool RETURNS reaches the client
    with ``isError=True`` — one authority decides the flag, so
    NotFound/Invalid/Conflict/Failed agree with PermissionDenied and
    with raised denials.
    """

    async def _fake_dispatch(name, arguments, *, principal=None):
        return variant

    monkeypatch.setattr(main_app, "dispatch_tool_call", _fake_dispatch)

    result = await _call_through_framework()

    assert result.isError is True, (
        f"a RETURNED {type(variant).__name__} must reach the client with "
        f"isError=True; got isError={result.isError!r}"
    )


@pytest.mark.asyncio
async def test_returned_ok_stays_not_iserror(monkeypatch) -> None:
    """A successful ``Ok`` result must keep ``isError=False`` — the fix
    must not flip successes into errors.
    """

    async def _fake_dispatch(name, arguments, *, principal=None):
        return Ok(message="done", data={"k": "v"})

    monkeypatch.setattr(main_app, "dispatch_tool_call", _fake_dispatch)

    result = await _call_through_framework()

    assert result.isError is False, (
        f"an Ok result must stay isError=False; got isError={result.isError!r}"
    )
    # Both the prose summary and the JSON payload survive (F015 contract).
    assert result.content[0].text == "done"


@pytest.mark.asyncio
async def test_raised_authrejected_still_iserror(monkeypatch) -> None:
    """Regression: the RAISED-``AuthRejected`` path (decorator-gated
    tools) must keep ``isError=True``. This is the fidelity the fix
    brings the RETURNED path up to; it must not regress.
    """

    async def _fake_dispatch(name, arguments, *, principal=None):
        raise AuthRejected(reason="admin token required")

    monkeypatch.setattr(main_app, "dispatch_tool_call", _fake_dispatch)

    result = await _call_through_framework()

    assert result.isError is True, (
        "a RAISED AuthRejected must reach the client with isError=True; "
        f"got isError={result.isError!r}"
    )
