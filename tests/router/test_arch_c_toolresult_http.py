"""Arch-deepening candidate C — the ONE ToolResult→HTTP adapter.

Two copies of the ToolResult-variant → HTTP-status mapping used to
live inline: ``app/_dispatch_helpers._dispatch_through_tool`` and the
register-agent route in ``app/routers/agents.py``. They DISAGREED —
``PermissionDenied`` mapped to 403 in the dispatcher but 401 in the
register route. Candidate C collapses both onto
:func:`agent_mcp.core.tool_result.tool_result_to_http`.

Tiebreak (locked): ``PermissionDenied → 403`` (authenticated but
forbidden — a capability the caller lacks). ``401`` stays reserved for
missing/invalid credentials, which the auth middleware returns upstream
before dispatch.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_mcp.app.rest_principal import RestPrincipal
from agent_mcp.app.routers import agents as agents_router
from agent_mcp.core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    tool_result_to_http,
)

# ── Table test: each variant → its status exactly once ───────────


def test_tool_result_to_http_status_table() -> None:
    """Every ``ToolResult`` variant maps to its locked HTTP status.

    Pins the ONE authority so a future edit that reintroduces a
    divergent copy (e.g. PermissionDenied→401) fails here.
    """
    cases = [
        (Ok(message="ok"), 200),
        (NotFound(resource="task", identifier="42"), 404),
        (PermissionDenied(reason="not the author"), 403),
        (Invalid(message="bad", field="name"), 400),
        (Conflict(reason="dup"), 409),
        (Failed(message="boom"), 500),
    ]
    got = {type(result).__name__: tool_result_to_http(result)[0]
           for result, _ in cases}
    expected = {type(result).__name__: status for result, status in cases}
    assert got == expected

    # PermissionDenied is the divergence this candidate closes: 403, not
    # 401. 401 is reserved for missing/invalid credentials handled by the
    # auth middleware upstream of dispatch.
    assert tool_result_to_http(PermissionDenied(reason="x"))[0] == 403


def test_tool_result_to_http_permission_denied_body() -> None:
    """The canonical body carries the reason under ``message`` (the key
    every REST consumer reads) at status 403."""
    status, body = tool_result_to_http(PermissionDenied(reason="nope"))
    assert status == 403
    assert body["message"] == "nope"
    assert body["success"] is False


def test_tool_result_to_http_failed_body_is_generic() -> None:
    """``Failed`` never leaks its raw message to the client (SEC-R8-1)."""
    status, body = tool_result_to_http(Failed(message="sqlite: table agents ..."))
    assert status == 500
    assert body["message"] == "Operation failed"
    assert "sqlite" not in json.dumps(body)


# ── Register-agent regression: PermissionDenied → 403 (was 401) ──

pytestmark_async = pytest.mark.asyncio


class _FakeRouteRequest:
    """Minimal stand-in for the Request the register handler reads."""

    def __init__(self, payload: dict[str, Any], *, method: str = "POST") -> None:
        self.method = method
        self._payload = payload

    async def body(self) -> bytes:
        return json.dumps(self._payload).encode()


@pytest.mark.asyncio
async def test_register_agent_permission_denied_maps_to_403(monkeypatch):
    """The register route returns 403 (not 401) when the tool impl
    denies on capability grounds.

    RED against pre-candidate-C code: the route's hand-rolled ladder
    mapped ``PermissionDenied → 401``. GREEN once the status routes
    through the shared :func:`tool_result_to_http` adapter.
    """

    async def _denying_impl(arguments, *, principal):
        return PermissionDenied(reason="viewer lacks agents.register")

    monkeypatch.setattr(
        agents_router, "register_agent_tool_impl", _denying_impl,
    )

    resp = await agents_router.register_agent_dashboard_api_route(
        _FakeRouteRequest({"name": "worker-1", "role": "worker"}),
        # operator-tier admit (no forwarding carrier) — mirrors the
        # r14 default-operator-tier fixture.
        auth=RestPrincipal(kind="operator_bearer"),
    )

    assert resp.status_code == 403
    payload = json.loads(bytes(resp.body))
    assert payload["message"] == "viewer lacks agents.register"
