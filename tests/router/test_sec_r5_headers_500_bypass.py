"""SD-R5-1: unhandled-exception 500s must NOT bypass the security-
headers middleware.

``security_headers_middleware`` is the outermost middleware and
documents an "every response, including errors" contract. Before the
round-5 fix it only caught ``web.HTTPException``. Any OTHER exception
(a bare ``ValueError`` from malformed multipart at
``login.py:330 await request.post()``, a ``TypeError`` in a handler,
…) propagated past it to aiohttp's core 500 renderer, which never
runs ``_apply_headers``. The resulting 500 leaked aiohttp's
version-disclosing ``Server: Python/… aiohttp/…`` banner and dropped
every hardened header (CSP, X-Frame-Options, nosniff, COOP, CORP,
Cache-Control: no-store, Referrer-Policy).

These tests drive a non-HTTPException THROUGH the real middleware and
assert the 500 is stamped like every other error response — and that
the exception detail never reaches the wire.
"""

from __future__ import annotations

import pytest
from aiohttp import web


pytestmark = pytest.mark.asyncio


def _assert_hardened_500(resp) -> None:
    """Every hardened header ``_apply_headers`` adds must be present,
    and the ``Server`` banner must be stripped of version fingerprints."""
    assert resp.status == 500
    # Server banner neutralised — no framework/version fingerprint.
    server = resp.headers.get("Server", "")
    assert server == "agent-mcp", server
    assert "aiohttp" not in server.lower()
    assert "python" not in server.lower()
    # Full hardened header set.
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert (
        resp.headers.get("Referrer-Policy")
        == "strict-origin-when-cross-origin"
    )
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp
    assert resp.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
    assert resp.headers.get("Cross-Origin-Resource-Policy") == "same-origin"
    assert resp.headers.get("Cache-Control") == "no-store"


async def test_valueerror_500_carries_security_headers(
    aiohttp_client_cls,
) -> None:
    """A handler that raises a bare ``ValueError`` must produce a 500
    that still carries the full hardened header set + stripped banner,
    and whose body does NOT echo the exception text."""
    from aiohttp.test_utils import TestServer

    from agent_mcp.router.security_headers import security_headers_middleware

    secret_marker = "SECRET_EXCEPTION_DETAIL_xyzzy"

    async def boom(request: web.Request) -> web.Response:  # pragma: no cover
        raise ValueError(secret_marker)

    app = web.Application(middlewares=[security_headers_middleware])
    app.router.add_get("/boom", boom)

    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        client = aiohttp_client_cls(server)
        await client.start_server()
        try:
            resp = await client.get("/boom")
            _assert_hardened_500(resp)
            body = await resp.text()
            # The exception detail must never reach the wire.
            assert secret_marker not in body
        finally:
            await client.close()
    finally:
        await server.close()


@pytest.mark.no_auth_seed_session
async def test_malformed_multipart_login_is_clean_401_not_500(
    aiohttp_client, router_app,
) -> None:
    """UNAUTH trigger: POST /agent-mcp/login with a malformed multipart
    body makes ``await request.post()`` raise a bare ``ValueError``.

    Round-5 hardened the resulting 500 so the outermost security-headers
    middleware still stamped it. PF-R21-1 then closed the vector at the
    source: ``login_post_handler`` now wraps ``request.post()`` in
    ``except (ValueError, UnicodeDecodeError)`` and folds a malformed
    body into the ordinary invalid-login path, so this no longer 500s at
    all — it returns a clean, hardened 401 (no version-banner leak, full
    header set). The middleware's non-HTTPException 500-stamping contract
    stays covered by ``test_valueerror_500_carries_security_headers``.
    """
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data=b"x",
        headers={"Content-Type": "multipart/form-data; boundary="},
    )
    # No longer a 500 — the parse failure is caught and returned as a
    # clean invalid-login 401.
    assert resp.status == 401, await resp.text()
    # The 401 still carries the full hardened header set + stripped banner.
    server = resp.headers.get("Server", "")
    assert server == "agent-mcp", server
    assert "aiohttp" not in server.lower()
    assert "python" not in server.lower()
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp
    assert resp.headers.get("Cache-Control") == "no-store"
