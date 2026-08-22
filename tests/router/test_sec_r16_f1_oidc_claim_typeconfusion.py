"""Security R16-F1 (MEDIUM, CONFIRMED) — OIDC claim type-confusion crashes
the SSO login callback.

FINDING: ``sso.handle_oidc_callback`` reads ``email = claims.get("email")``
and ``preferred_username = claims.get("preferred_username") or
claims.get("sub")`` with NO type check. OIDC claims are untyped/optional
per spec -- a misconfigured IdP (a multi-valued LDAP/SCIM attribute
serialised as a JSON array/object, or a numeric ``sub``) can send a
non-``str`` value. This crashes with an uncaught ``AttributeError``
(``'dict' object has no attribute 'encode'`` / ``'lower'``) or
``sqlite3.ProgrammingError`` deep in ``identity.create_user``,
``sso._sanitise_username``, and ``identity.find_linkable_user_by_email`` --
all of which assume ``str`` with no guard.

The sibling ``groups_claim`` in the SAME function already has an
``isinstance(groups_claim, list)`` guard -- this closes the same class of
gap for ``email``/``preferred_username``/``sub`` by mirroring that
pattern: a badly-typed claim degrades to "claim absent" instead of
propagating an unhandled exception.

Each test below reproduces one live crash site, then (post-fix) asserts
the clean, typed outcome.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


# ── Unit-level: each individual crash site ─────────────────────────


async def test_identity_create_user_rejects_dict_email(router_app) -> None:
    """A dict email must raise the typed InvalidEmailError, not a bare
    AttributeError from ``dict.encode()`` inside the pre-check."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    with pytest.raises(identity.InvalidEmailError):
        identity.create_user(
            username="typeconfuse1",
            password="longenoughpassword",
            email={"nested": "object"},
        )
    assert identity.get_user_by_username("typeconfuse1") is None


async def test_identity_create_user_rejects_int_email(router_app) -> None:
    """An int email must raise the typed InvalidEmailError, not a bare
    AttributeError from ``int.encode()`` inside the pre-check."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    with pytest.raises(identity.InvalidEmailError):
        identity.create_user(
            username="typeconfuse2",
            password="longenoughpassword",
            email=12345,
        )
    assert identity.get_user_by_username("typeconfuse2") is None


async def test_sanitise_username_rejects_dict() -> None:
    """A dict ``preferred_username``/``sub`` must not crash
    ``_sanitise_username``'s ``.lower()`` call -- it degrades to the
    same "no usable name" fallback the function already returns for an
    all-punctuation input."""
    from agent_mcp.router import sso

    assert sso._sanitise_username({"nested": "object"}) == "user"


async def test_identity_find_linkable_user_by_email_rejects_dict(
    router_app,
) -> None:
    """A dict email must not reach the unguarded sqlite bind inside
    ``find_linkable_user_by_email`` -- it short-circuits to "no match"
    instead of raising a raw ``sqlite3.ProgrammingError``/
    ``sqlite3.InterfaceError``."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    assert identity.find_linkable_user_by_email({"nested": "object"}) is None


# ── Full callback-level: claims carrying a type-confused value ─────


_FAKE_ISSUER = "https://idp.example.test"
_FAKE_CLIENT_ID = "agent-mcp-rp"
_FAKE_CLIENT_SECRET = "rp-secret-value"


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _fake_id_token(claims: dict[str, Any]) -> str:
    header = {"alg": "none", "typ": "JWT"}
    return ".".join([
        _b64url_nopad(json.dumps(header).encode()),
        _b64url_nopad(json.dumps(claims).encode()),
        "",
    ])


_FAKE_DISCOVERY = {
    "issuer": _FAKE_ISSUER,
    "authorization_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/token",
    "userinfo_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/userinfo",
    "jwks_uri": f"{_FAKE_ISSUER}/protocol/openid-connect/certs",
    "id_token_signing_alg_values_supported": ["RS256"],
}


@pytest.fixture
def sso_oidc_env(router_env, monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Mirrors ``test_sso_oidc.py``'s fixture of the same name -- kept
    local (rather than imported) so this file has no cross-file test
    fixture coupling."""
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


def _patch_idp(monkeypatch: pytest.MonkeyPatch, *, id_token_claims: dict):
    import sys
    sso = sys.modules["agent_mcp.router.sso"]

    monkeypatch.setattr(
        sso, "_fetch_oidc_metadata", lambda _issuer: dict(_FAKE_DISCOVERY),
    )
    token_bundle = {
        "access_token": "fake-access",
        "token_type": "Bearer",
        "expires_in": 300,
        "id_token": _fake_id_token(id_token_claims),
        "refresh_token": "fake-refresh",
    }
    monkeypatch.setattr(
        sso, "_exchange_code_for_tokens",
        lambda *args, **kw: token_bundle,
    )
    monkeypatch.setattr(
        sso, "_decode_id_token",
        lambda token, metadata, client_id, nonce=None: id_token_claims,
    )


async def _drive_callback(client, monkeypatch, *, claims: dict) -> object:
    _patch_idp(monkeypatch, id_token_claims=claims)
    init = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(init.headers["Location"]).query
    )["state"][0]
    return await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "fake-auth-code", "state": state},
        allow_redirects=False,
    )


async def test_oidc_callback_survives_dict_email_claim(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """A misconfigured IdP sending a JSON object for ``email`` (e.g. a
    multi-valued LDAP/SCIM attribute serialised verbatim) must not crash
    the callback with an uncaught AttributeError -- it degrades to
    "email claim absent" and the login still completes via JIT-create
    keyed on ``preferred_username``."""
    from agent_mcp.router import identity

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": "user-dict-email",
        "email": {"nested": "object"},
        "preferred_username": "dictemailuser",
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    row = identity.get_user_by_username("dictemailuser")
    assert row is not None, "expected JIT-created user 'dictemailuser'"
    assert row["email"] is None


async def test_oidc_callback_survives_int_email_claim(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """A misconfigured IdP sending a bare number for ``email`` must not
    crash the callback -- same degrade-to-absent posture as the dict
    case."""
    from agent_mcp.router import identity

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": "user-int-email",
        "email": 12345,
        "preferred_username": "intemailuser",
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    row = identity.get_user_by_username("intemailuser")
    assert row is not None, "expected JIT-created user 'intemailuser'"
    assert row["email"] is None


async def test_oidc_callback_survives_dict_preferred_username_claim(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """A misconfigured IdP sending a JSON object for
    ``preferred_username`` must not crash ``_sanitise_username`` -- the
    callback coerces the bad-typed claim to absent and falls back to
    ``sub`` for the JIT-create username (mirrors the pre-existing
    ``preferred_username or sub`` fallback for a genuinely MISSING
    claim)."""
    from agent_mcp.router import identity

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": "user-dict-username",
        "email": None,
        "preferred_username": {"nested": "object"},
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    # No exception escaped; a user was created under the sub-derived
    # fallback slug rather than the (rejected) dict claim.
    row = identity.get_user_by_username("user-dict-username")
    assert row is not None, "expected JIT-created user under sub-fallback slug"
