"""Security response headers on every router response (H3).

The router served no security headers before this change. These
tests pin the middleware output: nosniff, X-Frame-Options: DENY, a
CSP with frame-ancestors 'none' + object-src 'none', a referrer
policy, and HSTS gated on HTTPS (present on an https-simulated
request, absent on plain HTTP).
"""

from __future__ import annotations

from typing import ClassVar

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
    ``headers`` mapping, a ``.url.scheme``, and (since R6-F1 gated
    ``_request_is_https`` on ``rate_limit.request_from_trusted_proxy``)
    a ``.remote`` peer IP. Kept plain-HTTP with no forwarding header so
    no HSTS is added regardless of trust."""

    class _URL:
        scheme = "http"

    headers: ClassVar[dict] = {}
    url = _URL()
    remote = "203.0.113.7"


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


# ── OBS7 class-sweep: HSTS gate must trust XFP only from a trusted
# proxy (R6-F1) ──────────────────────────────────────────────────────
#
# ``_request_is_https`` is the last of 5 XFH/XFP trust-boundary
# consumers (alongside ``login.cookie_secure_flag``,
# ``login._external_origin``, ``mount.external_origin``,
# ``sso._default_redirect_url``) to receive the ``request_from_trusted_
# proxy`` gate. Before the fix it read ``X-Forwarded-Proto`` directly
# from ANY peer, so an untrusted client could spoof ``https`` to force
# HSTS onto a plain-HTTP response — or spoof ``http`` to strip HSTS
# from a genuinely-secure deployment, opening a downgrade window on a
# later connection. Mirrors ``test_login_flow.py``'s
# ``_mocked_request`` helper for the untrusted-vs-trusted-peer shape.


def _mocked_request(remote: str | None, headers: dict[str, str]):
    """A GET request with a chosen peer IP and headers.

    ``remote=None`` models a UDS peer (empty ``request.remote``) that the
    trusted-proxy check accepts, and carries a REAL ``socketpair``
    endpoint: UDS trust is decided by ``SO_PEERCRED`` (the peer runs as
    the router's own uid), not by the absence of a peer address — see
    ``tests/router/uds_peer.py``. A dotted-quad models a direct
    (untrusted) client hit. The mocked request's own scheme is plain
    HTTP (no TLS metadata attached), matching the underlying connection
    whose apparent scheme an untrusted peer must not be able to override.
    """
    from unittest import mock

    from aiohttp.test_utils import make_mocked_request

    from tests.router.uds_peer import uds_peer_socket

    transport = mock.Mock()
    peername = (remote, 40000) if remote else None
    sock = None if remote else uds_peer_socket()
    transport.get_extra_info = lambda key, default=None: (
        peername if key == "peername"
        else sock if key == "socket"
        else default
    )
    return make_mocked_request(
        "GET", "/agent-mcp/login", headers=headers, transport=transport,
    )


async def test_request_is_https_ignores_spoofed_xfp_from_untrusted_peer() -> None:
    """RED (pre-fix): an untrusted direct peer spoofing
    ``X-Forwarded-Proto: https`` over a plain-HTTP connection must NOT
    flip ``_request_is_https`` to True — the underlying transport
    scheme wins."""
    from agent_mcp.router.security_headers import _request_is_https

    req = _mocked_request(
        "203.0.113.7", {"X-Forwarded-Proto": "https"},
    )
    assert _request_is_https(req) is False


async def test_apply_headers_no_hsts_on_spoofed_xfp_from_untrusted_peer() -> None:
    """Same bug, exercised through the real consumer: HSTS must stay
    absent when a spoofed XFP arrives from an untrusted peer over
    plain HTTP."""
    from aiohttp import web

    from agent_mcp.router.security_headers import _apply_headers

    req = _mocked_request(
        "203.0.113.7", {"X-Forwarded-Proto": "https"},
    )
    resp = web.Response()
    _apply_headers(resp, req)
    assert resp.headers.get("Strict-Transport-Security") is None


async def test_request_is_https_honours_xfp_from_trusted_proxy() -> None:
    """Happy path preserved: a trusted proxy peer (loopback / UDS by
    default) forwarding ``X-Forwarded-Proto: https`` still flips
    ``_request_is_https`` to True."""
    from agent_mcp.router.security_headers import _request_is_https

    req = _mocked_request(
        "127.0.0.1", {"X-Forwarded-Proto": "https"},
    )
    assert _request_is_https(req) is True


async def test_request_is_https_honours_xfp_from_uds_peer() -> None:
    """A UDS / in-process peer (empty ``request.remote``) is trusted
    loopback by construction, same as the other 4 OBS7-gated
    consumers."""
    from agent_mcp.router.security_headers import _request_is_https

    req = _mocked_request(None, {"X-Forwarded-Proto": "https"})
    assert _request_is_https(req) is True


async def test_request_is_https_falls_back_to_transport_scheme_untrusted() -> None:
    """An untrusted peer with NO forwarding header at all still falls
    back correctly to the real (plain-HTTP) transport scheme."""
    from agent_mcp.router.security_headers import _request_is_https

    req = _mocked_request("203.0.113.7", {})
    assert _request_is_https(req) is False


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
