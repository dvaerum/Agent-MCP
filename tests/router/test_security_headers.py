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
async def test_cross_origin_isolation_headers_present(
    aiohttp_client, router_app,
) -> None:
    """COOP + CORP defense-in-depth headers land on normal responses.

    Cross-Origin-Opener-Policy: same-origin isolates the browsing
    context group (blocks cross-origin window handles / Spectre-style
    side channels); Cross-Origin-Resource-Policy: same-origin blocks
    other origins from embedding the router's responses as a resource.
    Both sit alongside the existing frame-ancestors / X-Frame-Options
    clickjacking defences.
    """
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/login")
    assert resp.status == 200
    assert resp.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
    assert resp.headers.get("Cross-Origin-Resource-Policy") == "same-origin"


@pytest.mark.no_auth_seed_session
async def test_cross_origin_isolation_headers_on_401_response(
    aiohttp_client, router_app,
) -> None:
    """COOP + CORP must land on the unauthenticated 401 path too — the
    middleware stamps error responses, same as the other headers."""
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
    )
    assert resp.status == 401
    assert resp.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
    assert resp.headers.get("Cross-Origin-Resource-Policy") == "same-origin"


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
async def test_cache_control_no_store_on_login(
    aiohttp_client, router_app,
) -> None:
    """SC-1: the login page must carry ``Cache-Control: no-store`` so an
    auth surface can't land in bfcache / a shared cache."""
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/login")
    assert resp.status == 200
    assert resp.headers.get("Cache-Control") == "no-store"


@pytest.mark.no_auth_seed_session
async def test_cache_control_no_store_on_api_401(
    aiohttp_client, router_app,
) -> None:
    """SC-1: authed-API 401 JSON must not be cacheable either."""
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
    )
    assert resp.status == 401
    assert resp.headers.get("Cache-Control") == "no-store"


async def test_apply_headers_preserves_explicit_cache_control() -> None:
    """SC-1: the static dashboard handlers set their own
    ``Cache-Control`` (``immutable`` for hash-named assets) BEFORE the
    middleware runs; ``_apply_headers`` must not clobber it."""
    from aiohttp import web

    from agent_mcp.router.security_headers import _apply_headers

    resp = web.Response(
        headers={"Cache-Control": "public, max-age=31536000, immutable"}
    )
    _apply_headers(resp, _FakeHttpRequest())
    assert (
        resp.headers.get("Cache-Control")
        == "public, max-age=31536000, immutable"
    )


class _FakeHttpRequest:
    """Minimal stand-in exposing what ``_apply_headers`` reads: a
    ``headers`` mapping and a ``.url.scheme``. Kept plain-HTTP so no
    HSTS is added."""

    class _URL:
        scheme = "http"

    headers: dict = {}
    url = _URL()


@pytest.mark.no_auth_seed_session
async def test_server_banner_stripped_of_versions(
    aiohttp_client, router_app,
) -> None:
    """SC-2 / SD-3: the ``Server`` header must NOT disclose framework
    versions (aiohttp defaults to ``Python/… aiohttp/…``). The
    middleware overwrites it with a neutral, version-free banner."""
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/login")
    assert resp.status == 200
    server = resp.headers.get("Server", "")
    assert "aiohttp" not in server.lower()
    assert "python" not in server.lower()
    assert server == "agent-mcp"


@pytest.mark.no_auth_seed_session
async def test_server_banner_stripped_on_401(
    aiohttp_client, router_app,
) -> None:
    """SC-2 / SD-3: the neutral ``Server`` banner must land on the error
    path too (aiohttp stamps its default on HTTPException responses)."""
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
    )
    assert resp.status == 401
    server = resp.headers.get("Server", "")
    assert "aiohttp" not in server.lower()
    assert server == "agent-mcp"


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
