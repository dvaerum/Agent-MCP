"""Security R19-F1 (MEDIUM, CONFIRMED) — R18-F1's OIDC subject-key retag
breaks reconciliation for every pre-existing SSO user.

FINDING: R18-F1 (#708) changed the persisted ``users.sso_subject``
reconciliation key format from ``oidc:<iss>:<sub>`` to
``oidc:<iss>:<type>:<sub>`` to fix a scalar-collision bug, but did not
add a fallback lookup for rows that still carry the OLD, untagged
format -- every real SSO user who logged in before the fix shipped.

On such a user's next login: ``find_or_create_sso_user`` step 1 (exact
tagged-key lookup) misses. Step 2 (verified-email link) explicitly
EXCLUDES rows with a non-NULL ``sso_subject`` (by design, to close the
R17-F1-era account-takeover vector), so it can't rescue them either --
even with a verified email. Step 3 JIT-creates a brand-new account,
orphaning the original row's sysadmin bit and ACLs.

Fix: ``find_or_create_sso_user`` also tries the legacy untagged key as
a fallback when the tagged lookup misses, and self-heals the row to
the new tagged format on a hit (mirrors the existing
``stamp_sso_subject_if_absent`` migrate-forward pattern).
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


# ── R19-F1: legacy-format sso_subject must still reconcile ─────────


async def test_old_format_subject_reconciles_not_duplicates(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """RED (pre-fix): a user with a PRE-R18-F1, untagged ``sso_subject``
    (``oidc:<iss>:<sub>``) must reconcile to the SAME row on their next
    login -- not mint a duplicate JIT-created account -- even though
    the current code computes the NEW tagged key for the lookup.

    Reproduces the live exploit exactly: create the row directly with
    the legacy key (as an old deployment's DB would already have it),
    then drive a real callback with the matching ``(iss, sub)`` and
    ``email_verified=True`` (the MOST favorable case for step-2
    reconciliation, which is blocked by design since the row already
    carries a non-NULL ``sso_subject``)."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    legacy_subject = f"oidc:{_FAKE_ISSUER}:alice-sub-1"
    user_id = identity.create_user(
        username="alice",
        password=None,
        email="alice@example.test",
        password_hash=None,
        is_sysadmin=True,
        sso_subject=legacy_subject,
    )

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": "alice-sub-1",
        "email": "alice@example.test",
        "email_verified": True,
        "preferred_username": "alice",
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    # Must NOT have minted "alice-2" -- that's the duplicate-account bug.
    assert identity.get_user_by_username("alice-2") is None

    alice = identity.get_user_by_id(user_id)
    assert alice is not None
    assert alice["last_login_at"] is not None, (
        "the ORIGINAL alice row must have been the one reconciled to "
        "(touch_last_login called on it) -- not orphaned in favour of "
        "a freshly JIT-created row"
    )
    assert alice["is_sysadmin"] == 1 or alice["is_sysadmin"] is True, (
        "the original sysadmin bit must survive reconciliation"
    )


async def test_old_format_subject_self_heals_to_new_format(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """After the fallback fires once for an old-format row, the row's
    ``sso_subject`` must be re-stamped to the new tagged format -- so
    subsequent logins hit the fast (non-fallback) path directly."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    legacy_subject = f"oidc:{_FAKE_ISSUER}:bob-sub-1"
    user_id = identity.create_user(
        username="bob",
        password=None,
        email="bob@example.test",
        password_hash=None,
        sso_subject=legacy_subject,
    )

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": "bob-sub-1",
        "email": "bob@example.test",
        "email_verified": True,
        "preferred_username": "bob",
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    bob = identity.get_user_by_id(user_id)
    assert bob is not None
    assert bob["sso_subject"] == f"oidc:{_FAKE_ISSUER}:str:bob-sub-1", (
        "the row must be re-stamped to the new tagged format on the "
        "fallback hit (self-heal)"
    )

    # A second login (same claims) must now reconcile via the FAST
    # (tagged, non-fallback) path -- still the same row, no duplicate.
    cb2 = await _drive_callback(client, monkeypatch, claims={
        "sub": "bob-sub-1",
        "email": "bob@example.test",
        "email_verified": True,
        "preferred_username": "bob",
        "groups": [],
    })
    assert cb2.status in (302, 303), await cb2.text()
    assert identity.get_user_by_username("bob-2") is None
    bob_again = identity.get_user_by_id(user_id)
    assert bob_again is not None
    assert bob_again["sso_subject"] == bob["sso_subject"]


async def test_new_format_subject_still_works_no_regression(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """R18-F1 regression guard: a row ALREADY carrying the new tagged
    format must keep reconciling via the direct (non-fallback) lookup
    -- the fallback must not interfere with the already-fixed path."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    tagged_subject = f"oidc:{_FAKE_ISSUER}:str:carol-sub-1"
    user_id = identity.create_user(
        username="carol",
        password=None,
        email="carol@example.test",
        password_hash=None,
        sso_subject=tagged_subject,
    )

    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client, monkeypatch, claims={
        "sub": "carol-sub-1",
        "email": "carol@example.test",
        "email_verified": True,
        "preferred_username": "carol",
        "groups": [],
    })
    assert cb.status in (302, 303), await cb.text()

    assert identity.get_user_by_username("carol-2") is None
    carol = identity.get_user_by_id(user_id)
    assert carol is not None
    assert carol["sso_subject"] == tagged_subject


async def test_scalar_type_collision_protection_unaffected(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
) -> None:
    """R16-F1/R17-F1/R18-F1 regression guard: two DIFFERENT scalar
    types whose ``str()`` forms collide (``sub=True`` / ``sub="True"``)
    must still produce DIFFERENT rows -- the legacy fallback must not
    reopen the R18-F1 collision by matching one claimant's login
    against the untagged form of a DIFFERENT type's key."""
    from agent_mcp.router import identity

    client = await aiohttp_client(router_app)

    cb1 = await _drive_callback(client, monkeypatch, claims={
        "sub": True,
        "preferred_username": "boolcollideuser",
        "groups": [],
    })
    assert cb1.status in (302, 303), await cb1.text()

    cb2 = await _drive_callback(client, monkeypatch, claims={
        "sub": "True",
        "preferred_username": "strcollideuser",
        "groups": [],
    })
    assert cb2.status in (302, 303), await cb2.text()

    row_bool = identity.get_user_by_username("boolcollideuser")
    row_str = identity.get_user_by_username("strcollideuser")
    assert row_bool is not None
    assert row_str is not None
    assert row_bool["user_id"] != row_str["user_id"]
    assert row_bool["sso_subject"] != row_str["sso_subject"]


async def test_oidc_subject_legacy_matches_pre_r18f1_format() -> None:
    """Unit-level: the legacy-key builder must reproduce the EXACT
    pre-R18-F1 untagged format, since it exists solely to match
    whatever an old row already has stored."""
    from agent_mcp.router import sso

    assert (
        sso._oidc_subject_legacy("https://idp.example.test", "abc-123")
        == "oidc:https://idp.example.test:abc-123"
    )
    assert sso._oidc_subject_legacy("https://idp.example.test", None) is None
    assert sso._oidc_subject_legacy(None, "abc-123") is None
