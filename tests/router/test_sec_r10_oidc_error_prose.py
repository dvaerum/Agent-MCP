"""SD-R10-1: OIDC login/callback error bodies MUST NOT reflect ``str(e)``.

Round-10 LOW info-disclosure finding + whole-class sweep. The OIDC
login (``/agent-mcp/sso/login``) and callback (``/agent-mcp/sso/callback``)
handlers previously returned the raw exception string to the
(unauthenticated) browser:

  * ``text=f"OIDC discovery fetch failed: {e}"``  — leaked the issuer /
    ``.well-known`` URL and network-topology / DNS / TLS specifics.
  * ``text=f"OIDC token exchange failed: {e}"``   — leaked the token-
    endpoint URL and the IdP's error prose.
  * ``text=f"OIDC id_token decode failed: {e}"``  — leaked JWKS URL /
    validation internals.

This is the same error-hygiene class the round-7…9 loop closed on the
routers / dispatch / RAG / orchestrator surfaces; the SSO surface was
missed. The fix genericizes each client-facing body to a static string
while retaining full detail server-side via ``logger.exception(...)``.

Each test forces an error arm to raise an exception whose message
embeds a SENTINEL secret URL, then asserts:

  * the HTTP response body is the generic string (no sentinel, no
    ``str(e)`` fragment), AND
  * the sentinel WAS captured in the server-side log record (detail is
    retained for the operator).
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


_FAKE_ISSUER = "https://idp.example.test"
_FAKE_CLIENT_ID = "agent-mcp-rp"
_FAKE_CLIENT_SECRET = "rp-secret-value"

# Sentinels embedded in the forced exceptions. The response body must
# NEVER contain these; the server-side log MUST.
_SENTINEL_DISCOVERY_URL = (
    "https://secret-issuer.internal.corp/.well-known/openid-configuration"
)
_SENTINEL_TOKEN_URL = "https://secret-issuer.internal.corp/oauth/token-xchg"
_SENTINEL_DECODE_MSG = "jwks-fetch-blew-up-at-https://secret-jwks.internal/certs"

_FAKE_DISCOVERY = {
    "issuer": _FAKE_ISSUER,
    "authorization_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/token",
    "userinfo_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/userinfo",
    "jwks_uri": f"{_FAKE_ISSUER}/protocol/openid-connect/certs",
    "id_token_signing_alg_values_supported": ["RS256"],
}


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _fake_id_token(claims: dict[str, Any]) -> str:
    header = {"alg": "none", "typ": "JWT"}
    return ".".join([
        _b64url_nopad(json.dumps(header).encode()),
        _b64url_nopad(json.dumps(claims).encode()),
        "",
    ])


@pytest.fixture
def sso_oidc_env(router_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Configure OIDC env vars + secret file + stub discovery (happy path)."""
    secret_file = tmp_path / "oidc.secret"
    secret_file.write_text(_FAKE_CLIENT_SECRET)
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_ISSUER", _FAKE_ISSUER)
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_CLIENT_ID", _FAKE_CLIENT_ID)
    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", str(secret_file),
    )
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_PROVIDER_NAME", "Test IdP")

    import sys
    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is None:
        import importlib
        sso = importlib.import_module("agent_mcp.router.sso")
    sso._reset_cache_for_tests()
    monkeypatch.setattr(
        sso, "_fetch_oidc_metadata", lambda _issuer: dict(_FAKE_DISCOVERY),
    )
    return secret_file


def _sso_module():
    import sys
    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is None:
        import importlib
        sso = importlib.import_module("agent_mcp.router.sso")
    return sso


async def _init_flow(client):
    """Drive /sso/login; return (state, response) so the callback can run."""
    init = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    assert init.status in (302, 303), await init.text()
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(init.headers["Location"]).query
    )["state"][0]
    return state


def _assert_no_leak(body: str):
    """The response body must contain none of the sentinel secrets."""
    for sentinel in (
        _SENTINEL_DISCOVERY_URL, _SENTINEL_TOKEN_URL, _SENTINEL_DECODE_MSG,
        "secret-issuer.internal.corp", "secret-jwks.internal",
    ):
        assert sentinel not in body, (
            f"response body leaked {sentinel!r}: {body!r}"
        )


# ── /sso/login discovery failure ────────────────────────────────────


async def test_login_discovery_error_body_is_generic(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch, caplog,
):
    """/sso/login discovery failure returns a generic 502, no URL leak."""
    import requests
    sso = _sso_module()

    def _boom(_issuer):
        raise requests.HTTPError(
            f"502 Server Error for url: {_SENTINEL_DISCOVERY_URL}"
        )

    monkeypatch.setattr(sso, "_fetch_oidc_metadata", _boom)

    client = await aiohttp_client(router_app)
    with caplog.at_level(logging.ERROR, logger="agent_mcp.router.sso"):
        resp = await client.get(
            "/agent-mcp/sso/login", allow_redirects=False,
        )
    assert resp.status == 502
    body = await resp.text()
    _assert_no_leak(body)
    # The detail WAS retained server-side.
    assert _SENTINEL_DISCOVERY_URL in caplog.text or any(
        _SENTINEL_DISCOVERY_URL in (r.exc_text or "")
        for r in caplog.records
    ), "issuer URL detail must be logged server-side"


# ── /sso/callback discovery failure ─────────────────────────────────


async def test_callback_discovery_error_body_is_generic(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch, caplog,
):
    """/sso/callback discovery failure returns a generic 502, no URL leak."""
    import requests
    sso = _sso_module()

    client = await aiohttp_client(router_app)
    state = await _init_flow(client)

    # Now make discovery fail on the callback leg.
    def _boom(_issuer):
        raise requests.HTTPError(
            f"connection refused: {_SENTINEL_DISCOVERY_URL}"
        )

    monkeypatch.setattr(sso, "_fetch_oidc_metadata", _boom)

    with caplog.at_level(logging.ERROR, logger="agent_mcp.router.sso"):
        cb = await client.get(
            "/agent-mcp/sso/callback",
            params={"code": "c", "state": state},
            allow_redirects=False,
        )
    assert cb.status == 502
    body = await cb.text()
    _assert_no_leak(body)
    assert _SENTINEL_DISCOVERY_URL in caplog.text or any(
        _SENTINEL_DISCOVERY_URL in (r.exc_text or "")
        for r in caplog.records
    ), "issuer URL detail must be logged server-side"
    assert "agent_mcp_session" not in cb.headers.get("Set-Cookie", "")


# ── /sso/callback token-exchange failure ────────────────────────────


async def test_callback_token_exchange_error_body_is_generic(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch, caplog,
):
    """Token-exchange failure returns a generic 502, no token-URL leak."""
    sso = _sso_module()

    client = await aiohttp_client(router_app)
    state = await _init_flow(client)

    def _boom(*args, **kwargs):
        raise RuntimeError(
            f"token endpoint POST failed: {_SENTINEL_TOKEN_URL}"
        )

    monkeypatch.setattr(sso, "_exchange_code_for_tokens", _boom)

    with caplog.at_level(logging.ERROR, logger="agent_mcp.router.sso"):
        cb = await client.get(
            "/agent-mcp/sso/callback",
            params={"code": "c", "state": state},
            allow_redirects=False,
        )
    assert cb.status == 502
    body = await cb.text()
    _assert_no_leak(body)
    assert _SENTINEL_TOKEN_URL in caplog.text or any(
        _SENTINEL_TOKEN_URL in (r.exc_text or "")
        for r in caplog.records
    ), "token endpoint detail must be logged server-side"
    assert "agent_mcp_session" not in cb.headers.get("Set-Cookie", "")


# ── /sso/callback id_token decode failure ───────────────────────────


async def test_callback_id_token_decode_error_body_is_generic(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch, caplog,
):
    """id_token decode failure returns a generic 502, no exception leak."""
    sso = _sso_module()

    client = await aiohttp_client(router_app)
    state = await _init_flow(client)

    monkeypatch.setattr(
        sso, "_exchange_code_for_tokens",
        lambda *a, **k: {"id_token": _fake_id_token({"sub": "u"})},
    )

    def _boom(token, metadata, client_id, nonce=None):
        raise ValueError(_SENTINEL_DECODE_MSG)

    monkeypatch.setattr(sso, "_decode_id_token", _boom)

    with caplog.at_level(logging.ERROR, logger="agent_mcp.router.sso"):
        cb = await client.get(
            "/agent-mcp/sso/callback",
            params={"code": "c", "state": state},
            allow_redirects=False,
        )
    assert cb.status == 502
    body = await cb.text()
    _assert_no_leak(body)
    assert _SENTINEL_DECODE_MSG in caplog.text or any(
        _SENTINEL_DECODE_MSG in (r.exc_text or "")
        for r in caplog.records
    ), "id_token decode detail must be logged server-side"
    assert "agent_mcp_session" not in cb.headers.get("Set-Cookie", "")
