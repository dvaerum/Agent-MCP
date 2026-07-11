"""AZ-R14-1 (round 14): the REST ``POST /api/<project>/agents/register``
route must thread the forwarding caller's REAL HMAC-signed ``project_role``
into the ``operator_session`` Principal it builds — not a hard-coded
``"operator"``.

Background
----------
``register_agent_dashboard_api_route`` was the single per-project REST route
that constructed its own ``operator_session`` Principal inline (with
``project_role="operator"`` / ``sysadmin=False``) instead of routing through
``_dispatch_helpers._build_route_principal``. It therefore bypassed the
round-5 AC-R5-1 forwarding-role threading: a forwarding VIEWER reaching this
route (should the router method-gate or cookie-authorize ever be bypassed)
would receive the full operator bundle — including ``agents.register`` —
regardless of true authority.

The fix threads the real signed ``(project_role, sysadmin)`` via
``deps.forwarding_route_role()`` (the task-local carrier
``require_operator_session`` arms on the forwarding branch), mirroring
``_build_route_principal``. A forwarding VIEWER now yields a viewer-role
Principal whose capability set the tool's own gate denies; a genuine
forwarding operator (and the cookie / operator-tier bearer paths, which
report ``None``) keep the historical operator-tier bundle.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from agent_mcp.app import deps
from agent_mcp.app.deps import forwarding_route_role, require_operator_session
from agent_mcp.app.routers import agents as agents_router
from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok
from tests.harness import make_principal


pytestmark = pytest.mark.asyncio


# ── Fakes ──────────────────────────────────────────────────────────


class _State:
    def __init__(self, principal: Optional[Principal]) -> None:
        self.principal = principal


class _FakeAuthRequest:
    """Stand-in the ``require_operator_session`` dep reads to admit the
    forwarding caller (mirrors ``test_sec_r5_rest_role_fidelity``)."""

    def __init__(self, principal: Optional[Principal]) -> None:
        self.state = _State(principal)
        self.cookies: dict[str, str] = {}
        self.method = "POST"
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}

    async def body(self) -> bytes:  # pragma: no cover - forwarding path returns first
        return b""


class _FakeRouteRequest:
    """Stand-in for the Request the register route handler reads: it needs
    ``.method`` and an awaitable ``.body()`` carrying the JSON payload."""

    def __init__(self, payload: dict[str, Any], *, method: str = "POST") -> None:
        self.method = method
        self._payload = payload

    async def body(self) -> bytes:
        return json.dumps(self._payload).encode()


def _forwarding_principal(role: Optional[str], *, user_id: str = "op-1") -> Principal:
    """A ``forwarding_header`` Principal as the auth middleware would build
    it — carrying the operator's REAL signed ``project_role``."""
    return make_principal(
        kind="forwarding_header",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role=role,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


@pytest.fixture(autouse=True)
def _reset_forwarding_carrier():
    """Isolate the module-level ContextVar across tests."""
    token = deps._forwarding_route_role.set(None)
    try:
        yield
    finally:
        deps._forwarding_route_role.reset(token)


# ── Tests ──────────────────────────────────────────────────────────


async def test_register_forwarding_viewer_principal_is_viewer_role(monkeypatch):
    """A forwarding VIEWER driven through the register route must produce a
    viewer-role Principal — so the tool's ``agents.register`` gate denies it.

    RED on origin/main: the route hard-coded ``project_role="operator"``,
    handing the viewer the full operator bundle (incl ``agents.register``).
    """
    auth = await require_operator_session(
        _FakeAuthRequest(_forwarding_principal("viewer"))
    )
    assert auth["kind"] == "forwarding"
    assert forwarding_route_role() == ("viewer", False)

    captured: dict[str, Any] = {}

    async def _fake_impl(arguments, *, principal):
        captured["principal"] = principal
        return Ok(message="ok", data={})

    monkeypatch.setattr(agents_router, "register_agent_tool_impl", _fake_impl)

    await agents_router.register_agent_dashboard_api_route(
        _FakeRouteRequest({"name": "worker-1", "role": "worker"}),
        auth=auth,
    )

    built = captured["principal"]
    assert built.kind == "operator_session"
    assert built.project_role == "viewer"          # NOT "operator"
    assert built.sysadmin is False
    # The point of the fix: the tool's own capability gate now DENIES.
    assert not built.has_capability("agents.register")


async def test_register_forwarding_viewer_denied_end_to_end():
    """End-to-end: a forwarding VIEWER hitting the register route with the
    REAL tool impl is denied by the ``agents.register`` capability gate —
    the route returns 403 (``PermissionDenied``) before any DB access.

    RED on origin/main: the hard-coded operator Principal passes the gate,
    so the route does NOT deny at all.

    Status note (arch-deepening candidate C): this assertion previously
    pinned 401 — the register route's hand-rolled ladder mapped
    ``PermissionDenied → 401``, diverging from the shared dispatcher's
    403. That divergence was the bug; the route now routes the status
    through the ONE ``tool_result_to_http`` adapter, so an
    authenticated-but-forbidden caller gets 403 (401 stays reserved for
    missing/invalid credentials the auth middleware rejects upstream).
    """
    auth = await require_operator_session(
        _FakeAuthRequest(_forwarding_principal("viewer"))
    )
    assert forwarding_route_role() == ("viewer", False)

    resp = await agents_router.register_agent_dashboard_api_route(
        _FakeRouteRequest({"name": "worker-1", "role": "worker"}),
        auth=auth,
    )

    assert resp.status_code == 403


async def test_register_forwarding_operator_principal_unaffected(monkeypatch):
    """Regression: a genuine forwarding operator still yields the operator
    bundle and registers — the fix threads the real role, it doesn't
    downgrade operators (the common path)."""
    auth = await require_operator_session(
        _FakeAuthRequest(_forwarding_principal("operator"))
    )
    assert forwarding_route_role() == ("operator", False)

    captured: dict[str, Any] = {}

    async def _fake_impl(arguments, *, principal):
        captured["principal"] = principal
        return Ok(
            message="Agent registered.",
            data={"agent_id": "worker-1", "token": "tok", "agent_role": "worker"},
        )

    monkeypatch.setattr(agents_router, "register_agent_tool_impl", _fake_impl)

    resp = await agents_router.register_agent_dashboard_api_route(
        _FakeRouteRequest({"name": "worker-1", "role": "worker"}),
        auth=auth,
    )

    built = captured["principal"]
    assert built.project_role == "operator"
    assert built.has_capability("agents.register")
    assert resp.status_code == 200


async def test_register_no_forwarding_role_defaults_operator_tier(monkeypatch):
    """Cookie / operator-tier bearer admits leave the carrier unset, so the
    route keeps its historical operator-tier default (those paths are
    genuinely operator). Simulates the ``operator_bearer`` auth dict."""
    assert forwarding_route_role() is None

    captured: dict[str, Any] = {}

    async def _fake_impl(arguments, *, principal):
        captured["principal"] = principal
        return Ok(message="ok", data={"agent_id": "worker-1", "token": "t"})

    monkeypatch.setattr(agents_router, "register_agent_tool_impl", _fake_impl)

    resp = await agents_router.register_agent_dashboard_api_route(
        _FakeRouteRequest({"name": "worker-1", "role": "worker"}),
        auth={"kind": "operator_bearer", "user": None},
    )

    built = captured["principal"]
    assert built.project_role == "operator"
    assert built.sysadmin is False
    assert built.has_capability("agents.register")
    assert resp.status_code == 200
