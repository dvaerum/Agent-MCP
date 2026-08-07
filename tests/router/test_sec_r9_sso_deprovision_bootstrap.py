"""Round-9 SSO security findings (AC-R9-1, AC-R9-2).

AC-R9-1 — OIDC group DE-provisioning gap (additive-only sync).
  ``apply_group_mapping`` only ADDS local group_membership rows for
  currently-claimed IdP groups; it never removed rows for groups the
  user dropped out of. Because ``group_resolver`` derives sysadmin /
  project role transitively from those rows, a user removed from an IdP
  group kept the local privilege indefinitely. The fix RECONCILES the
  IdP-managed (``oidc:``-namespaced) memberships on every callback:
  memberships in ``oidc:`` groups the current claim no longer maps to
  are removed. MANUAL (non-``oidc:``) local grants are untouched — the
  schema carries no per-row provenance, so only the reserved ``oidc:``
  namespace (populated exclusively by the wildcard-JIT path) is safe to
  reconcile.

AC-R9-2 — OIDC first-user auto-sysadmin had no bootstrap gate.
  ``_create_passwordless_user`` promotes the first user in an empty
  table to sysadmin. The proxy-header path gates that on
  ``AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN`` (refusing the JIT when the
  table is empty and the flag is off, so the operator bootstraps via the
  setup wizard). OIDC had no equivalent, so the first IdP user to
  complete ``/sso/login`` silently became sysadmin. The fix mirrors the
  proxy gate behind ``AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN``.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


_FAKE_ISSUER = "https://idp.example.test"
_FAKE_CLIENT_ID = "agent-mcp-rp"
_FAKE_CLIENT_SECRET = "rp-secret-value"

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
    """Configure OIDC env + stub IdP discovery (mirrors test_sso_oidc)."""
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
    """Patch the IdP-facing seams to return canned claims (hermetic)."""
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
    }
    monkeypatch.setattr(
        sso, "_exchange_code_for_tokens", lambda *a, **k: token_bundle,
    )
    monkeypatch.setattr(
        sso, "_decode_id_token",
        lambda token, metadata, client_id, nonce=None: id_token_claims,
    )


async def _drive_callback(client):
    """Run one login→callback round-trip; return the callback response."""
    init = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    assert init.status in (302, 303), await init.text()
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(init.headers["Location"]).query
    )["state"][0]
    return await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "ok", "state": state},
        allow_redirects=False,
    )


def _direct_group_names(user_id: str) -> set[str]:
    """Group NAMES the user is a DIRECT member of (via group_membership)."""
    from agent_mcp.router import identity
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT g.name FROM group_membership gm "
            "JOIN groups g ON g.group_id = gm.group_id "
            "WHERE gm.member_user_id = ?",
            (user_id,),
        )
        return {r["name"] for r in cur.fetchall()}


def _insert_group(name: str, *, is_sysadmin: int = 0) -> str:
    import secrets

    from agent_mcp.router import identity
    gid = secrets.token_hex(8)
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (gid, name, is_sysadmin),
        )
        conn.commit()
    return gid


def _add_membership(group_id: str, user_id: str) -> None:
    from agent_mcp.router import identity
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.execute(
            "INSERT INTO group_membership "
            "(group_id, member_user_id, member_group_id, added_at) "
            "VALUES (?, ?, NULL, datetime('now'))",
            (group_id, user_id),
        )
        conn.commit()


def _passwordless_users() -> list[dict]:
    from agent_mcp.router import identity
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM users WHERE password_hash IS NULL"
            )
        ]


# ── AC-R9-1: de-provisioning / reconciliation ──────────────────────


@pytest.mark.no_seed_operator
async def test_shrinking_idp_claims_revoke_oidc_group_but_keep_manual(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """A second OIDC login whose ``groups`` claim dropped a group must
    REMOVE the corresponding ``oidc:`` membership, KEEP a still-claimed
    ``oidc:`` group, and leave a MANUAL (non-``oidc:``) local grant
    untouched.
    """
    from agent_mcp.router import identity
    identity.run_router_migrations_upgrade()
    # Seed a password operator so the table is non-empty (this scenario
    # is about de-provisioning an EXISTING user, not first-boot).
    identity.create_user(username="seed_op", password="x" * 12)

    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_GROUP_MAPPING", json.dumps({"*": ""}),
    )
    import sys
    sys.modules["agent_mcp.router.sso"]._reset_cache_for_tests()

    # First login: user is in BOTH IdP groups → oidc:team-a, oidc:team-b.
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "stable-user",
        "email": "frank@example.test",
        "preferred_username": "frank",
        "groups": ["team-a", "team-b"],
    })
    client = await aiohttp_client(router_app)
    cb1 = await _drive_callback(client)
    assert cb1.status in (302, 303), await cb1.text()

    user = identity.get_user_by_username("frank")
    assert user is not None
    uid = user["user_id"]
    names = _direct_group_names(uid)
    assert "oidc:team-a" in names
    assert "oidc:team-b" in names

    # Operator manually grants a NON-IdP local group.
    manual_gid = _insert_group("manual-team")
    _add_membership(manual_gid, uid)
    assert "manual-team" in _direct_group_names(uid)

    # Second login: dropped from team-b, still in team-a.
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "stable-user",
        "email": "frank@example.test",
        "preferred_username": "frank",
        "groups": ["team-a"],
    })
    cb2 = await _drive_callback(client)
    assert cb2.status in (302, 303), await cb2.text()

    names_after = _direct_group_names(uid)
    # Still-claimed IdP group survives.
    assert "oidc:team-a" in names_after, names_after
    # Dropped IdP group is de-provisioned.
    assert "oidc:team-b" not in names_after, (
        f"stale IdP group retained: {names_after!r}"
    )
    # Manual (non-IdP) grant MUST survive an SSO login.
    assert "manual-team" in names_after, (
        f"manual grant was wrongly reconciled away: {names_after!r}"
    )


@pytest.mark.no_seed_operator
async def test_unchanged_claims_are_idempotent(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """Re-login with identical claims keeps the same memberships (no churn)."""
    from agent_mcp.router import identity
    identity.run_router_migrations_upgrade()
    identity.create_user(username="seed_op", password="x" * 12)

    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_GROUP_MAPPING", json.dumps({"*": ""}),
    )
    import sys
    sys.modules["agent_mcp.router.sso"]._reset_cache_for_tests()

    claims = {
        "sub": "steady",
        "email": "grace@example.test",
        "preferred_username": "grace",
        "groups": ["ops"],
    }
    _patch_idp(monkeypatch, id_token_claims=claims)
    client = await aiohttp_client(router_app)
    assert (await _drive_callback(client)).status in (302, 303)
    uid = identity.get_user_by_username("grace")["user_id"]
    assert "oidc:ops" in _direct_group_names(uid)

    _patch_idp(monkeypatch, id_token_claims=claims)
    assert (await _drive_callback(client)).status in (302, 303)
    assert "oidc:ops" in _direct_group_names(uid)


# ── AC-R9-2: first-user bootstrap gate ─────────────────────────────
#
# NOTE ON TEST LEVEL: the OIDC callback is fronted by the empty-users
# redirect middleware, which bounces every empty-table /agent-mcp/*
# HTML request (including /sso/login and /sso/callback) to /setup. So
# the empty-table OIDC callback is UNREACHABLE via HTTP — the first
# admin is always bootstrapped through the setup wizard. The residual
# gap the finding names is the CODE-level promotion in
# ``_create_passwordless_user`` (reachable only if the callback ever
# runs against an empty table — a middleware bypass / future refactor).
# We therefore test that promotion gate at the function level (through
# ``find_or_create_sso_user``, exactly as the callback drives it) and
# assert the reachable 2nd-user case through the full HTTP flow.


def _find_or_create(*, sub, email, bootstrap_sysadmin=None):
    """Invoke ``find_or_create_sso_user`` the way the OIDC callback does.

    The callback threads ``bootstrap_sysadmin=cfg.default_is_sysadmin``.
    Passing ``bootstrap_sysadmin=None`` here omits the kwarg entirely so
    the DEFAULT (gate-unset) behaviour is exercised — which is also what
    keeps the RED assertions runnable against origin/main, where the
    kwarg does not exist yet.
    """
    import sys
    sso = sys.modules["agent_mcp.router.sso"]
    subject = sso._oidc_subject(_FAKE_ISSUER, sub)
    kwargs = {}
    if bootstrap_sysadmin is not None:
        kwargs["bootstrap_sysadmin"] = bootstrap_sysadmin
    return sso.find_or_create_sso_user(
        email=email,
        preferred_username=email.split("@")[0],
        subject=subject,
        email_verified=False,
        **kwargs,
    )


async def test_load_sso_config_reads_oidc_default_sysadmin_flag(
    sso_oidc_env, monkeypatch,
):
    """``AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN`` populates
    ``OIDCSettings.default_is_sysadmin`` (default False)."""
    import sys
    sso = sys.modules["agent_mcp.router.sso"]

    # Unset → default False.
    monkeypatch.delenv("AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN", raising=False)
    cfg = sso.load_sso_config()
    assert cfg.oidc is not None and cfg.oidc.default_is_sysadmin is False

    # Truthy → True.
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN", "true")
    cfg = sso.load_sso_config()
    assert cfg.oidc.default_is_sysadmin is True


@pytest.mark.no_seed_operator
async def test_first_oidc_user_not_auto_sysadmin_by_default(
    router_env, sso_oidc_env,
):
    """RED on origin/main: the first OIDC-minted user in an empty table
    was silently promoted to sysadmin. The gate now DEFAULTS OFF, so the
    first user is a plain (non-sysadmin) account unless the operator
    opts in via ``AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN``."""
    from agent_mcp.router import identity
    identity.run_router_migrations_upgrade()
    assert _passwordless_users() == []  # empty bootstrap state

    user = _find_or_create(sub="first", email="boss@corp.test")
    assert user is not None
    assert user["is_sysadmin"] == 0, (
        "first OIDC user must NOT auto-promote to sysadmin by default"
    )


@pytest.mark.no_seed_operator
async def test_first_oidc_user_is_sysadmin_when_bootstrap_opt_in(
    router_env, sso_oidc_env,
):
    """Gate SET → the first OIDC user IS sysadmin, so an operator who
    deliberately opts in is not locked out."""
    from agent_mcp.router import identity
    identity.run_router_migrations_upgrade()
    assert _passwordless_users() == []

    user = _find_or_create(
        sub="first", email="boss@corp.test", bootstrap_sysadmin=True,
    )
    assert user is not None
    assert user["is_sysadmin"] == 1


@pytest.mark.no_seed_operator
async def test_second_oidc_user_never_auto_sysadmin_function_level(
    router_env, sso_oidc_env,
):
    """A NON-empty table (2nd user) never auto-promotes to sysadmin even
    with the bootstrap gate opted in (the gate is first-user-only)."""
    from agent_mcp.router import identity
    identity.run_router_migrations_upgrade()
    # Pre-existing operator → table is non-empty for the 2nd user.
    identity.create_user(username="existing_admin", password="y" * 12)

    user = _find_or_create(
        sub="second", email="regular@corp.test", bootstrap_sysadmin=True,
    )
    assert user is not None
    assert user["is_sysadmin"] == 0, (
        "second OIDC user must NOT be auto-sysadmin"
    )


async def test_second_oidc_user_never_auto_sysadmin_over_http(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """End-to-end: with a seeded operator (non-empty table), an OIDC
    callback login mints a NON-sysadmin user through the full stack."""
    from agent_mcp.router import identity
    # The router fixtures seed a password sentinel operator, so the users
    # table is already non-empty and /sso/login is not bounced to /setup.
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "http-second",
        "email": "regular2@corp.test",
        "preferred_username": "regular2",
        "groups": [],
    })
    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client)
    assert cb.status in (302, 303), await cb.text()
    assert "agent_mcp_session" in cb.headers.get("Set-Cookie", "")

    row = identity.get_user_by_username("regular2")
    assert row is not None
    assert row["is_sysadmin"] == 0
