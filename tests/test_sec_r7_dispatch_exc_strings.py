"""SD-R7-1: generic error on the MCP-wire + REST tool-dispatch fallbacks.

Round 6 (#321) genericized ``str(e)``-on-500 reflection in the tasks /
messages / agents / settings ROUTERS, but did NOT touch the two shared
tool-dispatch surfaces that still ship ``str(e)`` for any UNCAUGHT
exception a tool body raises:

  1. MCP wire — ``dispatch_tool_call`` re-raises the raw exception
     (``tools/registry.py``); it propagates through
     ``mcp_call_tool_handler`` (which did not catch) into the MCP SDK's
     ``_make_error_result(str(e))``, rendering the raw message verbatim
     to any worker / manager bearer with ``isError=True``.
  2. REST dashboard adapter — ``_dispatch_through_tool``'s catch-all
     returned ``{"error": f"Tool dispatch failed: {e}", ...}`` at HTTP
     500 for the same re-raised exceptions.

A ``sqlite3.OperationalError`` / ``OSError`` carried by such an
exception embeds table / column names or filesystem paths — schema /
internals disclosure.

This suite drives a sensitive-looking exception through both surfaces
and asserts the CLIENT-visible result is a STATIC generic message (no
``str(e)`` / SQL / path leak), while keeping ``isError=True`` fidelity
on the MCP path. Regression guards pin that a deliberately-raised,
controlled ``AuthRejected`` still surfaces its intended message, and a
normal ``Ok`` result stays ``isError=False``.

RED against origin/main (raw message leaked); GREEN after the round-6
genericize pattern is extended to these two dispatch surfaces.
"""

from __future__ import annotations

import json as _json
import sqlite3

import mcp.types as mcp_types
import pytest

import agent_mcp.app._dispatch_helpers as dispatch_helpers
import agent_mcp.app.main_app as main_app
from agent_mcp.app.rest_principal import RestPrincipal
from agent_mcp.core.authorize import AuthRejected
from agent_mcp.core.tool_result import Ok

pytestmark = pytest.mark.asyncio


# Sentinels that look like leaked SQL / schema / filesystem internals. If
# any fragment reaches the client, the raw exception was reflected.
_SENTINEL_SQL = "no such column: secret_table.api_key"
_SENTINEL_PATH = "/var/lib/agent-mcp/.secrets/master.key"
_LEAK_FRAGMENTS = (
    "secret_table",
    "api_key",
    "no such column",
    _SENTINEL_PATH,
    ".secrets",
    "sqlite3",
    "OperationalError",
    "OSError",
)


def _assert_no_leak(blob: str) -> None:
    """The client-visible error must not carry any exception detail."""
    for frag in _LEAK_FRAGMENTS:
        assert frag not in blob, (
            f"error surface leaked exception detail {frag!r}: {blob}"
        )


async def _call_through_framework(
    name: str = "any_tool",
) -> mcp_types.CallToolResult:
    """Drive a ``tools/call`` through the framework's registered handler
    — exactly the code path a live MCP client hits — and return the
    resulting :class:`CallToolResult` (``isError`` and content included).
    """
    handler = main_app.mcp_app_instance.request_handlers[
        mcp_types.CallToolRequest
    ]
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


# ── MCP wire — mcp_call_tool_handler ─────────────────────────────────────


async def test_mcp_uncaught_exception_is_generic_and_iserror(monkeypatch) -> None:
    """A tool body that raises an unexpected ``sqlite3.OperationalError``
    carrying an SQL sentinel must reach the MCP client as a STATIC
    generic message with ``isError=True`` — the raw ``str(e)`` must not
    be rendered to the wire (CONFIRMED-live on origin/main: the SDK's
    ``_make_error_result(str(e))`` reflects it verbatim)."""

    async def _boom(name, arguments, *, principal=None):
        raise sqlite3.OperationalError(_SENTINEL_SQL)

    monkeypatch.setattr(main_app, "dispatch_tool_call", _boom)

    result = await _call_through_framework()

    assert result.isError is True, (
        "an uncaught tool exception must still reach the client with "
        f"isError=True; got isError={result.isError!r}"
    )
    blob = _json.dumps([b.text for b in result.content])
    _assert_no_leak(blob)
    assert any("Tool execution failed" in b.text for b in result.content), (
        f"expected generic 'Tool execution failed'; got {blob}"
    )


async def test_mcp_uncaught_oserror_path_is_generic(monkeypatch) -> None:
    """An ``OSError`` carrying a filesystem path must not leak the path
    to the MCP client."""

    async def _boom(name, arguments, *, principal=None):
        raise OSError(f"[Errno 13] Permission denied: {_SENTINEL_PATH!r}")

    monkeypatch.setattr(main_app, "dispatch_tool_call", _boom)

    result = await _call_through_framework()

    assert result.isError is True
    blob = _json.dumps([b.text for b in result.content])
    _assert_no_leak(blob)


async def test_mcp_raised_authrejected_message_preserved(monkeypatch) -> None:
    """Regression: a deliberately-raised, controlled ``AuthRejected`` is
    NOT genericized — its intended ``Unauthorized: ...`` message still
    surfaces with ``isError=True`` (round-3/4 fidelity)."""

    async def _reject(name, arguments, *, principal=None):
        raise AuthRejected(reason="Unauthorized: admin token required")

    monkeypatch.setattr(main_app, "dispatch_tool_call", _reject)

    result = await _call_through_framework()

    assert result.isError is True
    assert any("Unauthorized" in b.text for b in result.content), (
        f"controlled AuthRejected message must survive; got "
        f"{[b.text for b in result.content]!r}"
    )
    assert not any(
        "Tool execution failed" in b.text for b in result.content
    ), "controlled auth message must not be genericized"


async def test_mcp_ok_result_stays_not_iserror(monkeypatch) -> None:
    """Regression: a normal ``Ok`` result stays ``isError=False`` — the
    generic-error wrapping must not flip successes into errors."""

    async def _ok(name, arguments, *, principal=None):
        return Ok(message="done", data={"k": "v"})

    monkeypatch.setattr(main_app, "dispatch_tool_call", _ok)

    result = await _call_through_framework()

    assert result.isError is False, (
        f"an Ok result must stay isError=False; got {result.isError!r}"
    )
    assert result.content[0].text == "done"


# ── REST dashboard adapter — _dispatch_through_tool ──────────────────────


async def test_rest_uncaught_exception_is_generic(monkeypatch) -> None:
    """The REST adapter's catch-all 500 must be a STATIC generic message
    — no reflected ``str(e)`` (CONFIRMED-live on origin/main:
    ``f"Tool dispatch failed: {e}"``)."""

    async def _boom(name, arguments, *, principal=None):
        raise sqlite3.OperationalError(_SENTINEL_SQL)

    monkeypatch.setattr(dispatch_helpers, "dispatch_tool_call", _boom)

    resp = await dispatch_helpers._dispatch_through_tool(
        "some_tool",
        {},
        bearer_token=None,
        auth=RestPrincipal(kind="operator_bearer"),
    )
    assert resp.status_code == 500
    blob = resp.body.decode("utf-8")
    _assert_no_leak(blob)
    body = _json.loads(blob)
    assert body.get("error") == "Tool dispatch failed", body
    assert body.get("message") == "Tool dispatch failed", body


async def test_rest_authrejected_reason_preserved(monkeypatch) -> None:
    """Regression: a controlled ``AuthRejected`` still surfaces its
    intended reason at HTTP 403 (NOT genericized)."""

    async def _reject(name, arguments, *, principal=None):
        raise AuthRejected(reason="admin token required")

    monkeypatch.setattr(dispatch_helpers, "dispatch_tool_call", _reject)

    resp = await dispatch_helpers._dispatch_through_tool(
        "some_tool",
        {},
        bearer_token=None,
        auth=RestPrincipal(kind="operator_bearer"),
    )
    assert resp.status_code == 403
    body = _json.loads(resp.body.decode("utf-8"))
    assert body.get("message") == "admin token required", body
