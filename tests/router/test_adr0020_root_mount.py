"""ADR-0020: the router serves the same routes under /agent-mcp (tailnet)
AND at the host root (Traefik at mm.best.aau.dk), on one process.

The security-critical property: a root-aliased route is gated IDENTICALLY
to its /agent-mcp twin (the auth gate keys off mount.canonical_path). A
regression here = an unauthenticated bypass at the root front door.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

VER = {"Accept": "application/vnd.agent-mcp.v1+json"}


@pytest.mark.no_auth_seed_session
async def test_health_serves_at_both_mounts(aiohttp_client, router_app) -> None:
    """The public health probe answers at both the tailnet and root
    mounts (root alias wired + allow-listed)."""
    client = await aiohttp_client(router_app)
    tail = await client.get("/agent-mcp/api/router/health", headers=VER)
    root = await client.get("/api/router/health", headers=VER)
    assert tail.status == 200, await tail.text()
    assert root.status == 200, await root.text()
    assert (await root.json())["ok"] is True


@pytest.mark.no_auth_seed_session
async def test_operator_route_gated_at_root_no_bypass(
    aiohttp_client, router_app,
) -> None:
    """SECURITY: an operator-gated route with NO session must 401 at the
    root mount exactly as at the /agent-mcp mount. If the root alias
    skipped the gate this would 200 — the bypass this ADR must not open.
    """
    client = await aiohttp_client(router_app)
    tail = await client.get("/agent-mcp/api/router/projects", headers=VER)
    root = await client.get("/api/router/projects", headers=VER)
    assert tail.status == 401, await tail.text()
    assert root.status == 401, await root.text()


async def test_tailnet_front_door_authed(aiohttp_client, router_app) -> None:
    """The default client logs in via /agent-mcp/login (tailnet front
    door) → its /agent-mcp-scoped session reaches the tailnet route."""
    client = await aiohttp_client(router_app)  # auto-login via /agent-mcp/login
    tail = await client.get("/agent-mcp/api/router/projects", headers=VER)
    assert tail.status == 200, await tail.text()


@pytest.mark.no_auth_seed_session
async def test_root_front_door_login_then_authed_route(
    aiohttp_client, router_app,
) -> None:
    """End-to-end root front door: logging in at the ROOT /login mints a
    root-scoped (Path=/) cookie that then authorises a ROOT operator
    route. This is the mm.best.aau.dk flow, one process."""
    from tests.router.conftest import _SENTINEL_PASSWORD, _SENTINEL_USERNAME

    client = await aiohttp_client(router_app)
    login = await client.post(
        "/login",
        data={"username": _SENTINEL_USERNAME, "password": _SENTINEL_PASSWORD},
        allow_redirects=False,
    )
    assert login.status == 303, await login.text()
    # The Set-Cookie must be Path=/ so it's replayed on root paths.
    assert "Path=/" in login.headers.get("Set-Cookie", "")
    root = await client.get("/api/router/projects", headers=VER)
    assert root.status == 200, await root.text()


async def test_nested_asset_tail_route_resolves_at_root(router_app) -> None:
    """The {rest:.*} tail routes must be aliased with the regex intact —
    a NESTED path (multiple segments) must resolve to the asset handler
    at root, not 404 as a single-segment {rest} would."""
    from aiohttp.test_utils import make_mocked_request

    req = make_mocked_request("GET", "/assets/deep/nested/chunk.js")
    match = await router_app.router.resolve(req)
    assert match.http_exception is None, "nested /assets/... did not match"
    assert match.handler.__name__ == "dashboard_assets_handler"

    req2 = make_mocked_request("GET", "/app/proj/deep/route")
    match2 = await router_app.router.resolve(req2)
    assert match2.http_exception is None, "nested /app/<n>/... did not match"


async def test_html_landing_redirect_honours_mount(
    aiohttp_client, router_app,
) -> None:
    """The bare landing (html), for an AUTHED operator, redirects to the
    dashboard at the CLIENT's prefix. The default client is tailnet-
    authed → /agent-mcp/ redirects to /agent-mcp/app/..."""
    client = await aiohttp_client(router_app)  # tailnet-authed
    tail = await client.get(
        "/agent-mcp/", headers={"Accept": "text/html"}, allow_redirects=False,
    )
    assert tail.status in (302, 303, 307), await tail.text()
    assert tail.headers["Location"].startswith("/agent-mcp/app/"), \
        tail.headers["Location"]


@pytest.mark.no_auth_seed_session
async def test_login_form_action_honours_mount(
    aiohttp_client, router_app,
) -> None:
    """The rendered login form POSTs to the login page at the caller's
    mount — root → action="/login", tailnet → action="/agent-mcp/login".
    A hardcoded /agent-mcp action would set a /agent-mcp cookie on a root
    login, which the root redirect never sends → login loop."""
    client = await aiohttp_client(router_app)
    root = await (await client.get("/login")).text()
    tail = await (await client.get("/agent-mcp/login")).text()
    assert 'action="/login' in root, root[:600]
    assert 'action="/agent-mcp/login' in tail, tail[:600]


@pytest.mark.no_auth_seed_session
async def test_unauth_login_redirect_honours_mount(
    aiohttp_client, router_app,
) -> None:
    """An UNAUTHENTICATED html visit is bounced to the login page at the
    caller's mount: root → /login (stays at root, no /agent-mcp bounce);
    tailnet → /agent-mcp/login."""
    client = await aiohttp_client(router_app)
    root = await client.get(
        "/", headers={"Accept": "text/html"}, allow_redirects=False,
    )
    tail = await client.get(
        "/agent-mcp/", headers={"Accept": "text/html"}, allow_redirects=False,
    )
    assert root.status == 303, await root.text()
    assert root.headers["Location"].startswith("/login?next="), \
        root.headers["Location"]
    assert tail.headers["Location"].startswith("/agent-mcp/login?next="), \
        tail.headers["Location"]


@pytest.mark.no_auth_seed_session
async def test_root_landing_redirect_honours_root_mount(
    aiohttp_client, router_app,
) -> None:
    """The root front door: an operator authed at the root sees the bare
    ``/`` landing redirect to ``/app/...`` (no /agent-mcp prefix)."""
    from tests.router.conftest import _SENTINEL_PASSWORD, _SENTINEL_USERNAME

    client = await aiohttp_client(router_app)
    await client.post(
        "/login",
        data={"username": _SENTINEL_USERNAME, "password": _SENTINEL_PASSWORD},
        allow_redirects=False,
    )
    root = await client.get(
        "/", headers={"Accept": "text/html"}, allow_redirects=False,
    )
    assert root.status in (302, 303, 307), await root.text()
    assert root.headers["Location"].startswith("/app/"), \
        root.headers["Location"]
