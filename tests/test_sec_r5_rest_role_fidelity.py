"""AC-R5-1 (round 5): the REST dispatch path must carry the forwarding
caller's REAL HMAC-signed ``project_role`` — not a hard-coded
``"operator"`` — into the Principal it builds for an
``operator_session`` tool dispatch.

Background
----------
Two Principal-construction paths exist on the per-project backend:

* **MCP wire** (``main_app._build_principal_from_request``) correctly
  stamps ``project_role = forwarding_role`` — the operator's real
  signed viewer/operator role.
* **REST** (``/api/<project>/…``): ``require_operator_session`` read
  ``request.state.principal`` but returned only the operator *id*,
  dropping ``project_role``; ``_dispatch_helpers._build_route_principal``
  then rebuilt a fresh Principal with a hard-coded
  ``project_role="operator"`` (and ``sysadmin=False``).

So the REST path treated every forwarding caller as a full operator
regardless of their real signed role — a latent viewer→operator
escalation for any future GET route that dispatches a privileged
``operator_session`` tool. These tests pin the fix: a forwarding VIEWER
now yields a viewer-role Principal whose capability set the tool's own
gate denies, while a forwarding operator (and the cookie / bearer
paths) are unaffected.

Finding D (Phase 5) changed only WHERE the role travels: it rode a
module-level ``ContextVar`` (``deps._forwarding_route_role``) between
the dep and the dispatch helper, and is now a field on the
``RestPrincipal`` the dep returns, which the helper is handed directly.
The property under test is identical; the assertions read the field
instead of the carrier.
"""

from __future__ import annotations

import pytest

from agent_mcp.app import deps
from agent_mcp.app._dispatch_helpers import _build_route_principal
from agent_mcp.app.deps import (
    SESSION_COOKIE_NAME,
    require_operator_session,
)
from agent_mcp.app.rest_principal import RestPrincipal
from agent_mcp.core.principal import Principal
from tests.harness import make_principal

pytestmark = pytest.mark.asyncio


# ── Fakes ──────────────────────────────────────────────────────────


class _State:
    def __init__(self, principal: Principal | None) -> None:
        self.principal = principal


class _FakeRequest:
    """Minimal stand-in for the FastAPI Request the dep reads.

    ``require_operator_session`` consults ``.cookies``,
    ``.state.principal``, ``.headers``, ``.query_params`` and
    ``await .body()``; the forwarding branch returns before headers /
    body are touched.
    """

    def __init__(
        self,
        principal: Principal | None,
        *,
        cookies: dict[str, str] | None = None,
        method: str = "DELETE",
    ) -> None:
        self.state = _State(principal)
        self.cookies = cookies or {}
        self.method = method
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}

    async def body(self) -> bytes:  # pragma: no cover - forwarding path returns first
        return b""


def _forwarding_principal(role: str | None, *, user_id: str = "op-1") -> Principal:
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


# ── Tests ──────────────────────────────────────────────────────────


async def test_forwarding_viewer_yields_viewer_role_not_operator():
    """A forwarding VIEWER driven through the REST dispatch path must
    produce a viewer-role Principal — so the tool's capability gate
    denies the operator-only verbs (RED on origin/main: hard-coded
    ``project_role='operator'`` handed the viewer the full bundle)."""
    req = _FakeRequest(_forwarding_principal("viewer"))

    auth = await require_operator_session(req)

    assert auth.kind == "forwarding"
    # The dep threads the real role on the value it returns; the
    # dispatch seam reads it off that value, not off a module global.
    assert auth.route_role() == ("viewer", False)

    built = _build_route_principal(auth=auth)

    assert built is not None
    assert built.kind == "operator_session"
    assert built.project_role == "viewer"          # NOT "operator"
    assert built.sysadmin is False
    # The tool's own capability gate would now deny the privileged
    # operator_session verbs — the point of the fix.
    assert not built.has_capability("tasks.delete")
    assert not built.has_capability("agents.terminate")


async def test_forwarding_operator_yields_operator_role_unaffected():
    """A genuine forwarding operator keeps the operator bundle — the fix
    threads the real role, it doesn't downgrade operators."""
    req = _FakeRequest(_forwarding_principal("operator"))

    auth = await require_operator_session(req)

    assert auth.kind == "forwarding"
    assert auth.route_role() == ("operator", False)

    built = _build_route_principal(auth=auth)

    assert built.project_role == "operator"
    assert built.has_capability("tasks.delete")
    assert built.has_capability("agents.terminate")


async def test_no_forwarding_role_defaults_to_operator_tier():
    """Cookie / operator-tier bearer admits report no route role, so
    ``_build_route_principal`` keeps its historical operator-tier default
    (those paths are genuinely operator)."""
    cookie_auth = RestPrincipal(kind="session", user={"username": "op-cookie"})
    assert cookie_auth.route_role() is None

    built = _build_route_principal(auth=cookie_auth)

    assert built.project_role == "operator"
    assert built.sysadmin is False
    assert built.has_capability("tasks.delete")


async def test_cookie_admit_cannot_inherit_a_prior_forwarding_role(monkeypatch):
    """A forwarding-viewer admit followed by a cookie admit in the SAME
    task must not leak the viewer role into the cookie caller's dispatch.

    Pre-Finding-D this needed the dep to explicitly reset a task-local
    ContextVar at entry — an easy thing to drop. Now each admit returns
    its own value and the dispatch seam reads only the value it was
    handed, so staleness is unrepresentable rather than defended
    against. The regression is pinned all the same."""
    fwd_auth = await require_operator_session(
        _FakeRequest(_forwarding_principal("viewer"))
    )
    assert fwd_auth.route_role() == ("viewer", False)

    monkeypatch.setattr(
        deps,
        "_resolve_session_user",
        lambda sid: {"user_id": "u1", "username": "alice"},
    )
    # Wave 12 PR A: the authorize helper RETURNS the resolved
    # ``(project_role, sysadmin)``. This test only cares that the cookie
    # admit is independent of the forwarding one, so a benign admit
    # tuple suffices.
    monkeypatch.setattr(
        deps,
        "_authorize_session_for_project",
        lambda user, request: (None, False),
    )
    cookie_req = _FakeRequest(
        principal=None,
        cookies={SESSION_COOKIE_NAME: "sess-1"},
    )

    auth = await require_operator_session(cookie_req)

    assert auth.kind == "session"
    assert auth.route_role() is None
    # ... and the earlier forwarding admit is untouched by it.
    assert fwd_auth.route_role() == ("viewer", False)

    built = _build_route_principal(auth=auth)
    assert built.project_role == "operator"
    assert built.has_capability("tasks.delete")
