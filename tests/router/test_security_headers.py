"""Security response headers on every router response (H3).

The router served no security headers before this change. These
tests pin the middleware output: nosniff, X-Frame-Options: DENY, a
CSP with frame-ancestors 'none' + object-src 'none', a referrer
policy, and HSTS gated on HTTPS (present on an https-simulated
request, absent on plain HTTP).
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


def _seed_user() -> None:
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    identity.create_user(username="hdruser", password="pw")


@pytest.mark.no_auth_seed_session
async def test_security_headers_present_on_normal_response(
    aiohttp_client, router_app,
) -> None:
    _seed_user()
    client = await aiohttp_client(router_app)
    # /login is allow-listed, so no auth needed and we get a real 200.
    resp = await client.get("/agent-mcp/login")
    assert resp.status == 200
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert (
        resp.headers.get("Referrer-Policy")
        == "strict-origin-when-cross-origin"
    )
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "default-src 'self'" in csp


@pytest.mark.no_auth_seed_session
async def test_hsts_present_on_https_request(
    aiohttp_client, router_app,
) -> None:
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/login",
        headers={"X-Forwarded-Proto": "https"},
    )
    assert resp.status == 200
    hsts = resp.headers.get("Strict-Transport-Security", "")
    assert "max-age=63072000" in hsts
    assert "includeSubDomains" in hsts


@pytest.mark.no_auth_seed_session
async def test_hsts_absent_on_plain_http(
    aiohttp_client, router_app,
) -> None:
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/login",
        headers={"X-Forwarded-Proto": "http"},
    )
    assert resp.status == 200
    assert resp.headers.get("Strict-Transport-Security") is None


@pytest.mark.no_auth_seed_session
async def test_security_headers_on_401_response(
    aiohttp_client, router_app,
) -> None:
    """Headers must land even on the unauthenticated 401 path."""
    _seed_user()
    client = await aiohttp_client(router_app)
    # A gated API path with no cookie → 401 JSON; headers still apply.
    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
    )
    assert resp.status == 401
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "frame-ancestors 'none'" in resp.headers.get(
        "Content-Security-Policy", ""
    )


@pytest.mark.no_auth_seed_session
async def test_csp_override_via_env(
    aiohttp_client, router_module, monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_MCP_CSP", "default-src 'none'")
    app = router_module.make_app()
    _seed_user()
    client = await aiohttp_client(app)
    resp = await client.get("/agent-mcp/login")
    assert resp.headers.get("Content-Security-Policy") == "default-src 'none'"
