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
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from agent_mcp.app import deps
from agent_mcp.app._dispatch_helpers import _build_route_principal
from agent_mcp.app.deps import (
    SESSION_COOKIE_NAME,
    forwarding_route_role,
    require_operator_session,
)
from agent_mcp.core.principal import Principal


pytestmark = pytest.mark.asyncio


# ── Fakes ──────────────────────────────────────────────────────────


class _State:
    def __init__(self, principal: Optional[Principal]) -> None:
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
        principal: Optional[Principal],
        *,
        cookies: Optional[dict[str, str]] = None,
        method: str = "DELETE",
    ) -> None:
        self.state = _State(principal)
        self.cookies = cookies or {}
        self.method = method
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}

    async def body(self) -> bytes:  # pragma: no cover - forwarding path returns first
        return b""


def _forwarding_principal(role: Optional[str], *, user_id: str = "op-1") -> Principal:
    """A ``forwarding_header`` Principal as the auth middleware would build
    it — carrying the operator's REAL signed ``project_role``."""
    return Principal(
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
    """Isolate the module-level ContextVar across tests — ContextVar
    values set via ``.set()`` in one test persist into the next in the
    same task otherwise."""
    token = deps._forwarding_route_role.set(None)
    try:
        yield
    finally:
        deps._forwarding_route_role.reset(token)


# ── Tests ──────────────────────────────────────────────────────────


async def test_forwarding_viewer_yields_viewer_role_not_operator():
    """A forwarding VIEWER driven through the REST dispatch path must
    produce a viewer-role Principal — so the tool's capability gate
    denies the operator-only verbs (RED on origin/main: hard-coded
    ``project_role='operator'`` handed the viewer the full bundle)."""
    req = _FakeRequest(_forwarding_principal("viewer"))

    auth = await require_operator_session(req)

    assert auth["kind"] == "forwarding"
    # The dep now threads the real role via the task-local carrier
    # instead of dropping it (the return dict's shape is pinned by
    # test_sec_r4_operator_identity_race, so the role rides the carrier).
    assert forwarding_route_role() == ("viewer", False)

    built = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=auth["operator_id"],
    )

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

    assert auth["kind"] == "forwarding"
    assert forwarding_route_role() == ("operator", False)

    built = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=auth["operator_id"],
    )

    assert built.project_role == "operator"
    assert built.has_capability("tasks.delete")
    assert built.has_capability("agents.terminate")


async def test_no_forwarding_role_defaults_to_operator_tier():
    """Cookie / operator-tier bearer admits leave the carrier unset, so
    ``_build_route_principal`` keeps its historical operator-tier default
    (those paths are genuinely operator)."""
    assert forwarding_route_role() is None

    built = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id="op-cookie",
    )

    assert built.project_role == "operator"
    assert built.sysadmin is False
    assert built.has_capability("tasks.delete")


async def test_cookie_admit_resets_stale_forwarding_role(monkeypatch):
    """The dep resets the carrier at entry: a forwarding-viewer admit
    followed by a cookie admit in the same task must NOT leak the viewer
    role into the cookie caller's dispatch (cookie regression)."""
    # First a forwarding-viewer admit arms the carrier.
    await require_operator_session(_FakeRequest(_forwarding_principal("viewer")))
    assert forwarding_route_role() == ("viewer", False)

    # Then a cookie admit in the same task must clear it.
    monkeypatch.setattr(
        deps,
        "_resolve_session_user",
        lambda sid: {"user_id": "u1", "username": "alice"},
    )
    monkeypatch.setattr(
        deps,
        "_authorize_session_for_project",
        lambda user, request: None,
    )
    cookie_req = _FakeRequest(
        principal=None,
        cookies={SESSION_COOKIE_NAME: "sess-1"},
    )

    auth: dict[str, Any] = await require_operator_session(cookie_req)

    assert auth["kind"] == "session"
    assert forwarding_route_role() is None

    built = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id="alice",
    )
    assert built.project_role == "operator"
    assert built.has_capability("tasks.delete")
