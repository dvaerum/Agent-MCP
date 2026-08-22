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


# ── R17-F1: non-str ``sub`` must still reconcile identity ──────────
#
# R16-F1 (above) applied its str-only ``sub`` coercion to BOTH of
# ``sub``'s uses: the (genuinely str-only) preferred_username fallback,
# AND ``_oidc_subject``'s stable reconciliation key -- which only
# f-string-interpolates ``sub`` and never needed a str guard. That
# regressed a numeric (or bool/float) ``sub`` to a permanently-None
# subject, defeating subject-based reconciliation: every login from
# such an IdP re-minted a brand-new orphaned user instead of matching
# the existing one.


async def test_oidc_callback_reconciles_int_sub_across_logins(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """RED (pre-fix): two logins with the same (iss, sub=424242) and no
    email claim must reconcile to the SAME user row via a stable,
    non-None ``sso_subject`` -- not mint two distinct users."""
    from agent_mcp.router import identity

    client = await aiohttp_client(router_app)
    claims = {
        "sub": 424242,
        "preferred_username": "numericsubuser",
        "groups": [],
    }

    cb1 = await _drive_callback(client, monkeypatch, claims=dict(claims))
    assert cb1.status in (302, 303), await cb1.text()
    cb2 = await _drive_callback(client, monkeypatch, claims=dict(claims))
    assert cb2.status in (302, 303), await cb2.text()

    row1 = identity.get_user_by_username("numericsubuser")
    assert row1 is not None
    assert row1["sso_subject"] is not None, (
        "a numeric sub must still produce a stable, non-None "
        "sso_subject -- degrading it to None defeats reconciliation"
    )

    # The second login must NOT have minted a "numericsubuser-2"
    # collision row -- it must have matched row1 via sso_subject.
    assert identity.get_user_by_username("numericsubuser-2") is None
    row2 = identity.find_user_by_sso_subject(row1["sso_subject"])
    assert row2 is not None
    assert row2["user_id"] == row1["user_id"]


async def test_oidc_callback_int_sub_username_fallback_still_safe(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """The ORIGINAL R16-F1 fix must still hold: a non-str ``sub`` used
    as the preferred_username FALLBACK (no preferred_username claim at
    all) must not crash ``_sanitise_username`` -- it degrades cleanly
    to the generated "user" slug, independent of the (now-fixed, still
    non-None) subject reconciliation key."""
    from agent_mcp.router import identity

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": 999999,
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    # No preferred_username and a non-str sub -> _sanitise_username
    # never received a usable str, so the JIT username falls back to
    # the generated "user" slug (or its collision-suffixed sibling).
    row = identity.get_user_by_username("user")
    assert row is not None, "expected JIT-created user under fallback slug"
    assert row["sso_subject"] is not None


async def test_oidc_subject_missing_sub_unaffected(monkeypatch) -> None:
    """A genuinely absent ``sub`` (no sub at all) must still degrade
    ``_oidc_subject`` to None -- no regression to the pre-existing
    "no sub" path."""
    from agent_mcp.router import sso

    assert sso._oidc_subject("https://idp.example.test", None) is None


async def test_oidc_subject_str_sub_unaffected(monkeypatch) -> None:
    """A normal str ``sub`` must be unaffected by the R17-F1 fix."""
    from agent_mcp.router import sso

    assert (
        sso._oidc_subject("https://idp.example.test", "abc-123")
        == "oidc:https://idp.example.test:abc-123"
    )
