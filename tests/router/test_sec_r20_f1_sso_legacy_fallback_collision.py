"""Security R20-F1 (MEDIUM, CONFIRMED) — R19-F1's legacy-fallback lookup
reopens the exact scalar-type collision it claims can't reopen.

FINDING: R19-F1 (#709) added a legacy-format fallback to
``find_or_create_sso_user``: when the current tagged-key lookup misses,
it tries the legacy lookup key for ``(iss, sub)`` -- the pre-R18-F1 UNTAGGED
key format, which is, by construction, the exact non-type-discriminating
shape R16-F1/R17-F1/R18-F1 fixed (``str(1) == str("1")``,
``str(True) == str("True")``). On a legacy-key HIT, the row is
unconditionally re-stamped to the CURRENT caller's tagged key and
returned as the caller's identity -- with NO check that the current
login's ``sub`` TYPE matches whatever type the legacy row was originally
minted from (the legacy string can't record that, by definition).

Historical note (verified against git history for R16-F1/#704,
R17-F1/#705, R18-F1/#708): the reporting lane's "legacy rows can only
ever have been str-minted" premise does NOT hold. the key builder only
ever received a str-coerced ``sub`` through R16-F1, but R17-F1 (#705)
widened it to accept int/float/bool WHILE STILL using the untagged
format -- R18-F1 (#708) is what added the type tag. So during the
R17-F1 -> R18-F1 window a legacy (untagged) row could legitimately have
been minted from a non-str scalar. A "reject the fallback unless the
current sub is str" fix would therefore be UNSOUND (it would still let
a str claimant hijack a legacy row that was genuinely minted from a
same-string-content int/float/bool sub, and vice versa) -- hence this
fix takes the "refuse on ambiguity" direction instead: the fallback
lookup key is only offered (``SsoSubject.legacy_lookup_key`` returns non-None)
when the current sub's string form could NOT also have been produced by
a DIFFERENT accepted scalar type. See ``sso.SsoSubject.is_ambiguous``.

Live-confirmed exploit (this file's primary RED test): a genuine
pre-existing sysadmin row ``alice`` has legacy key ``oidc:<iss>:1``
(as if minted from ``sub="1"`` pre-R18-F1). A brand-new, unrelated
identity ``mallory`` logs in for the first time with ``sub=1`` as a
JSON INT -- the legacy lookup key for ``(iss, 1)`` is the IDENTICAL
string ``oidc:<iss>:1``. Pre-fix, the fallback matches and ``mallory``
is returned ``alice``'s ``user_id`` (and her ``is_sysadmin`` bit).
Post-fix, the ambiguous legacy key must never be offered as a fallback
lookup key, so ``mallory`` gets her OWN, non-privileged, JIT-created row
and ``alice``'s row is untouched.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

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


def _legacy_key(iss: object | None, sub: object | None) -> str | None:
    """The pre-R18-F1 UNTAGGED fallback key for ``(iss, sub)``, or None.

    ADR-0024: ``sso._oidc_subject_legacy`` is now
    ``SsoSubject.legacy_lookup_key()``, which still returns None for an
    unusable claim AND for an ambiguous sub (R20-F1). Assertions below
    are unchanged.
    """
    from agent_mcp.router.sso import SsoSubject

    subject = SsoSubject.from_claims(iss, sub)
    return subject.legacy_lookup_key() if subject is not None else None


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


# ── R20-F1: the legacy fallback must never hijack an ambiguous key ──


async def test_differently_typed_claimant_does_not_hijack_legacy_row(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """RED (pre-fix): the live exploit. A pre-existing sysadmin row
    ``alice`` carries the legacy key ``oidc:<iss>:1``. A brand-new
    claimant ``mallory`` logs in with ``sub=1`` as a JSON INT --
    the legacy lookup key is the IDENTICAL string. Pre-fix,
    the fallback matches and mallory is reconciled into (and inherits
    the sysadmin bit of) alice's account. Post-fix, mallory must get
    her OWN, non-privileged row and alice's row must be untouched."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    legacy_subject = f"oidc:{_FAKE_ISSUER}:1"
    alice_id = identity.create_user(
        username="alice",
        password=None,
        email="alice@example.test",
        password_hash=None,
        is_sysadmin=True,
        sso_subject=legacy_subject,
    )
    alice_before = identity.get_user_by_id(alice_id)
    assert alice_before is not None
    assert alice_before["last_login_at"] is None

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": 1,  # JSON int, not a string
        "preferred_username": "mallory",
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    mallory = identity.get_user_by_username("mallory")
    assert mallory is not None, "mallory must have gotten her own row"
    assert mallory["user_id"] != alice_id, (
        "mallory must NOT have been reconciled into alice's account"
    )
    assert not (mallory["is_sysadmin"] == 1 or mallory["is_sysadmin"] is True), (
        "mallory must NOT inherit alice's sysadmin bit"
    )

    # alice's row must be completely untouched -- no self-heal upgrade,
    # no touch_last_login from mallory's request.
    alice_after = identity.get_user_by_id(alice_id)
    assert alice_after is not None
    assert alice_after["sso_subject"] == legacy_subject, (
        "alice's row must not have been re-stamped by mallory's login"
    )
    assert alice_after["last_login_at"] is None, (
        "alice's row must not have been touched by mallory's login"
    )


async def test_ambiguous_bool_str_claimant_does_not_hijack_legacy_row(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """Same exploit shape, using the bool/str collision family
    (``str(True) == "True"``) instead of the int/str one."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    legacy_subject = f"oidc:{_FAKE_ISSUER}:True"
    victim_id = identity.create_user(
        username="victim",
        password=None,
        email="victim@example.test",
        password_hash=None,
        is_sysadmin=True,
        sso_subject=legacy_subject,
    )

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": True,  # JSON bool, not a string
        "preferred_username": "attacker",
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    attacker = identity.get_user_by_username("attacker")
    assert attacker is not None
    assert attacker["user_id"] != victim_id
    assert not (attacker["is_sysadmin"] == 1 or attacker["is_sysadmin"] is True)

    victim_after = identity.get_user_by_id(victim_id)
    assert victim_after is not None
    assert victim_after["sso_subject"] == legacy_subject


async def test_str_claimant_does_not_hijack_numeric_legacy_row(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """Mirror direction: a STR claimant must not hijack a legacy row
    whose key content could equally have been minted from a non-str
    scalar (e.g. a legacy row literally named ``"1"``)."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    legacy_subject = f"oidc:{_FAKE_ISSUER}:1"
    victim_id = identity.create_user(
        username="numvictim",
        password=None,
        email="numvictim@example.test",
        password_hash=None,
        is_sysadmin=True,
        sso_subject=legacy_subject,
    )

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": "1",  # JSON string "1" -- same string form as int 1
        "preferred_username": "strattacker",
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    attacker = identity.get_user_by_username("strattacker")
    assert attacker is not None
    assert attacker["user_id"] != victim_id
    assert not (attacker["is_sysadmin"] == 1 or attacker["is_sysadmin"] is True)

    victim_after = identity.get_user_by_id(victim_id)
    assert victim_after is not None
    assert victim_after["sso_subject"] == legacy_subject


# ── Unit-level: the ambiguity discriminator itself ─────────────────


def test_legacy_subject_ambiguity_discriminator() -> None:
    from agent_mcp.router.sso import SsoSubject

    # Non-str scalars are always ambiguous against a hypothetical str
    # of the same content.
    assert SsoSubject(_FAKE_ISSUER, 1).is_ambiguous() is True
    assert SsoSubject(_FAKE_ISSUER, 1.5).is_ambiguous() is True
    assert SsoSubject(_FAKE_ISSUER, True).is_ambiguous() is True
    assert SsoSubject(_FAKE_ISSUER, False).is_ambiguous() is True

    # str content that LOOKS like a canonical int/float/bool repr is
    # ambiguous.
    assert SsoSubject(_FAKE_ISSUER, "1").is_ambiguous() is True
    assert SsoSubject(_FAKE_ISSUER, "-42").is_ambiguous() is True
    assert SsoSubject(_FAKE_ISSUER, "1.5").is_ambiguous() is True
    assert SsoSubject(_FAKE_ISSUER, "0").is_ambiguous() is True
    assert SsoSubject(_FAKE_ISSUER, "True").is_ambiguous() is True
    assert SsoSubject(_FAKE_ISSUER, "False").is_ambiguous() is True

    # Genuine, non-numeric str subs (the real-world common case) are
    # unambiguous -- no other scalar type's str() can reproduce them.
    assert SsoSubject(_FAKE_ISSUER, "alice-sub-1").is_ambiguous() is False
    assert SsoSubject(_FAKE_ISSUER, "abc-123").is_ambiguous() is False
    assert SsoSubject(_FAKE_ISSUER, "007").is_ambiguous() is False  # not canonical int


def test_oidc_subject_legacy_withholds_ambiguous_keys() -> None:
    """``SsoSubject.legacy_lookup_key`` must return None (no fallback
    offered) for an ambiguous sub, and the normal untagged key for a
    safe one."""
    assert _legacy_key(_FAKE_ISSUER, 1) is None
    assert _legacy_key(_FAKE_ISSUER, "1") is None
    assert _legacy_key(_FAKE_ISSUER, True) is None
    assert _legacy_key(_FAKE_ISSUER, "True") is None
    assert (
        _legacy_key(_FAKE_ISSUER, "alice-sub-1")
        == f"oidc:{_FAKE_ISSUER}:alice-sub-1"
    )
