"""OIDC single-sign-on tests (Phase 3 Wave 3 of prancy-napping-pie).

The router gains a new auth mode driven by these env vars:

  AGENT_MCP_SSO_OIDC_ISSUER         — IdP discovery root.
  AGENT_MCP_SSO_OIDC_CLIENT_ID      — RP client id.
  AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE
                                    — file path with the client secret
                                      (matches the existing sops/file-
                                      based secret pattern; the value
                                      itself is never put in env).
  AGENT_MCP_SSO_OIDC_GROUP_MAPPING  — JSON ``{oidc_group: agentmcp_group}``
                                      with optional ``"*"`` wildcard
                                      escape that JIT-creates a group
                                      per claim.

When the issuer is set, the router exposes two new routes:

  GET  /agent-mcp/sso/login    → initiates the OAuth2 code+PKCE flow.
  GET  /agent-mcp/sso/callback → exchanges the code for tokens, decodes
                                 the id_token, JIT-creates the user
                                 (matched by ``email``), applies group
                                 mapping, mints a session cookie.

The login page (``GET /agent-mcp/login``) ALSO switches into "SSO mode"
when OIDC is active: the username/password form is replaced with a
single "Sign in with <provider>" button that links to ``/sso/login``.
The username/password form is retained ONLY for the setup-wizard
bootstrap path (first user) — covered by ``test_setup_wizard``.

Strategy: we don't spin up a real IdP — the tests monkey-patch the
discovery fetch + token-exchange to return JWT bodies we control.
This keeps the suite hermetic and CI-deterministic.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from joserfc.errors import JoseError

pytestmark = pytest.mark.asyncio


# ── Fake IdP helpers ────────────────────────────────────────────────


_FAKE_ISSUER = "https://idp.example.test"
_FAKE_CLIENT_ID = "agent-mcp-rp"
_FAKE_CLIENT_SECRET = "rp-secret-value"


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _group_names_for(group_ids: set[str]) -> set[str]:
    """Resolve a set of group_ids to the corresponding group names.

    The router stores ``groups.name`` UNIQUE; the SSO callback's
    apply_group_mapping uses that name as the dashboard-visible
    identifier, so the test assertions live in name-space, not id-
    space.
    """
    import sqlite3

    from agent_mcp.router import identity

    if not group_ids:
        return set()
    placeholders = ",".join("?" for _ in group_ids)
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            f"SELECT name FROM groups WHERE group_id IN ({placeholders})",
            tuple(group_ids),
        )
        return {row["name"] for row in cur.fetchall()}


def _fake_id_token(claims: dict[str, Any]) -> str:
    """Build an unsigned JWT (alg=none) for the OIDC callback fakes.

    Tests inject the decoded claims via a patched ``decode_id_token``
    helper in ``agent_mcp.router.sso``; the wire format here only has
    to look JWT-ish enough that the route accepts the response body.
    """
    header = {"alg": "none", "typ": "JWT"}
    return ".".join([
        _b64url_nopad(json.dumps(header).encode()),
        _b64url_nopad(json.dumps(claims).encode()),
        "",
    ])


_FAKE_DISCOVERY = {
    "issuer": _FAKE_ISSUER,
    "authorization_endpoint": (
        f"{_FAKE_ISSUER}/protocol/openid-connect/auth"
    ),
    "token_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/token",
    "userinfo_endpoint": (
        f"{_FAKE_ISSUER}/protocol/openid-connect/userinfo"
    ),
    "jwks_uri": f"{_FAKE_ISSUER}/protocol/openid-connect/certs",
    "id_token_signing_alg_values_supported": ["RS256"],
}


@pytest.fixture
def sso_oidc_env(
    router_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Configure OIDC env vars + secret file + stub IdP discovery.

    Discovery is patched unconditionally so the routes never reach the
    real network during a test. Individual tests still patch the
    token-exchange + id-token-decode seams to inject their claims.
    """
    secret_file = tmp_path / "oidc.secret"
    secret_file.write_text(_FAKE_CLIENT_SECRET)
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_ISSUER", _FAKE_ISSUER)
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_CLIENT_ID", _FAKE_CLIENT_ID)
    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", str(secret_file),
    )
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_PROVIDER_NAME", "Test IdP")

    # Patch the IdP discovery seam in-place on whichever sso module is
    # cached. The route handlers reference ``_fetch_oidc_metadata`` as
    # a bare global, so the patch MUST land on the module object the
    # handler functions close over (i.e. the one already loaded by
    # make_app() in the router_app fixture above us). Reset the SSO
    # config cache so the new env vars are re-read on the next call.
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
    """Patch the SSO module's IdP-facing surface for a hermetic flow.

    Three things get patched:

      * ``_fetch_oidc_metadata`` — return a fixed discovery doc.
      * ``_exchange_code_for_tokens`` — return the canned token bundle.
      * ``_decode_id_token`` — return the canned claims (skips the
        signature check; we exercise the signature path in a separate
        Authlib-level unit test, not the route-shape tests here).
    """
    import sys
    sso = sys.modules["agent_mcp.router.sso"]

    discovery = {
        "issuer": _FAKE_ISSUER,
        "authorization_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/token",
        "userinfo_endpoint": f"{_FAKE_ISSUER}/protocol/openid-connect/userinfo",
        "jwks_uri": f"{_FAKE_ISSUER}/protocol/openid-connect/certs",
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    monkeypatch.setattr(sso, "_fetch_oidc_metadata", lambda _issuer: discovery)
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


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.no_seed_operator
async def test_login_page_renders_sso_button_when_oidc_active(
    aiohttp_client, router_app, sso_oidc_env,
):
    """GET /agent-mcp/login shows the SSO button (no pw form) under OIDC."""
    # Seed an operator so the empty-users middleware doesn't bounce
    # us to /setup — that path is bootstrap-only and covered elsewhere.
    from agent_mcp.router import identity
    identity.run_router_migrations_upgrade()
    identity.create_user(username="sso_seed", password="x" * 12)

    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/login")
    assert resp.status == 200
    body = await resp.text()
    # SSO button MUST link to /sso/login and carry the provider name.
    assert "/agent-mcp/sso/login" in body
    assert "Test IdP" in body
    # Password form fields MUST be absent under OIDC mode.
    assert 'name="password"' not in body


async def test_sso_login_redirects_to_idp_with_pkce(
    aiohttp_client, router_app, sso_oidc_env,
):
    """GET /sso/login → 302/303 to the IdP's authorize endpoint w/ PKCE."""
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    assert resp.status in (302, 303)
    location = resp.headers["Location"]
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    # The authorize URL MUST point at the IdP, carry our client_id, a
    # PKCE challenge with method S256, and the openid scope.
    assert parsed.scheme == "https"
    assert parsed.netloc == "idp.example.test"
    assert parsed.path == "/protocol/openid-connect/auth"
    assert qs.get("client_id") == [_FAKE_CLIENT_ID]
    assert qs.get("response_type") == ["code"]
    assert qs.get("code_challenge_method") == ["S256"]
    assert "code_challenge" in qs
    assert "state" in qs
    # The scope MUST request id_token + group claims at minimum.
    scope = qs.get("scope", [""])[0]
    assert "openid" in scope
    # The redirect cookie that pins the per-flow state/PKCE verifier
    # MUST be set on the response so the callback can recover them.
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "agent_mcp_sso_flow" in set_cookie


async def test_sso_callback_creates_user_and_session(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """Callback decodes the id_token, JIT-creates the user, sets cookie."""
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "user-1",
        "email": "alice@example.test",
        "preferred_username": "Alice",
        "groups": [],
    })
    client = await aiohttp_client(router_app)
    init = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    flow_cookie = init.cookies.get("agent_mcp_sso_flow")
    assert flow_cookie is not None
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(init.headers["Location"]).query
    )["state"][0]

    # Mock the callback hit from the IdP.
    cb = await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "fake-auth-code", "state": state},
        allow_redirects=False,
    )
    # Must land back inside /agent-mcp/ with a session cookie.
    assert cb.status in (302, 303), await cb.text()
    set_cookie = cb.headers.get("Set-Cookie", "")
    assert "agent_mcp_session" in set_cookie

    # The JIT-created user MUST be queryable and the session
    # cookie MUST authenticate them on a subsequent request.
    from agent_mcp.router import identity
    row = identity.get_user_by_username("alice")
    assert row is not None, "expected JIT-created user 'alice'"
    assert row["email"] == "alice@example.test"
    # SSO users have no password — the row's password_hash is NULL.
    assert row["password_hash"] is None


async def test_sso_callback_matches_existing_user_by_email(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """A VERIFIED email matches an existing local user rather than re-creating.

    The email-link path is gated on ``email_verified is True`` (the
    account-takeover fix): a matching, IdP-asserted-verified email
    binds the SSO login to the pre-existing local (password) operator.
    """
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    existing_id = identity.create_user(
        username="bob",
        password="hunter2hunter2",
        email="bob@example.test",
    )
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "user-bob",
        "email": "bob@example.test",
        "email_verified": True,
        "preferred_username": "BobFromSSO",
        "groups": [],
    })
    client = await aiohttp_client(router_app)
    init = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    assert init.status in (302, 303), (
        f"unexpected init status {init.status}: {await init.text()}"
    )
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(init.headers["Location"]).query
    )["state"][0]
    cb = await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "ok", "state": state},
        allow_redirects=False,
    )
    assert cb.status in (302, 303), await cb.text()

    # The existing user MUST be reused — no duplicate by SSO username.
    assert identity.get_user_by_username("bobfromsso") is None
    row = identity.get_user_by_id(existing_id)
    assert row is not None
    # last_login_at was stamped.
    assert row["last_login_at"] is not None


async def test_sso_group_mapping_explicit(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """An explicit mapping ``oidc-group → amcp-group`` populates membership."""
    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_GROUP_MAPPING",
        json.dumps({"eng-backend": "backend-team"}),
    )
    from agent_mcp.router import group_resolver, identity
    identity.run_router_migrations_upgrade()
    # Pre-create the agent-mcp side group so the mapping has a target.
    import secrets
    import sqlite3
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, 'backend-team', 0, datetime('now'))",
            (secrets.token_hex(8),),
        )
        conn.commit()

    _patch_idp(monkeypatch, id_token_claims={
        "sub": "u",
        "email": "carol@example.test",
        "preferred_username": "carol",
        "groups": ["eng-backend", "unrelated-group"],
    })
    client = await aiohttp_client(router_app)
    init = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(init.headers["Location"]).query
    )["state"][0]
    cb = await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "ok", "state": state},
        allow_redirects=False,
    )
    assert cb.status in (302, 303), await cb.text()

    user = identity.get_user_by_username("carol")
    assert user is not None
    group_ids = group_resolver.resolve_user_groups(user["user_id"])
    group_names = _group_names_for(group_ids)
    assert "backend-team" in group_names
    # Unmapped OIDC group MUST NOT leak in as a new group.
    assert "unrelated-group" not in group_names


async def test_sso_group_mapping_wildcard_jit(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """The ``*`` wildcard mapping JIT-creates each unknown group.

    Wildcard-provisioned groups are namespaced under an ``oidc:``
    prefix so a claim value can never collide with (and inherit the
    caps of) a locally-managed group of the same slug — see
    ``test_sso_wildcard_group_does_not_bind_existing_privileged_group``.
    """
    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_GROUP_MAPPING",
        json.dumps({"*": ""}),
    )
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "u",
        "email": "dave@example.test",
        "preferred_username": "dave",
        "groups": ["Eng-Backend", "Ops Team"],
    })
    client = await aiohttp_client(router_app)
    init = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(init.headers["Location"]).query
    )["state"][0]
    cb = await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "ok", "state": state},
        allow_redirects=False,
    )
    assert cb.status in (302, 303), await cb.text()

    from agent_mcp.router import group_resolver, identity
    user = identity.get_user_by_username("dave")
    assert user is not None
    group_ids = group_resolver.resolve_user_groups(user["user_id"])
    group_names = _group_names_for(group_ids)
    # Names get sanitized (lowercase, spaces → dashes) AND namespaced
    # under ``oidc:`` so wildcard claims live in their own reserved
    # space, unable to collide with locally-managed groups.
    assert "oidc:eng-backend" in group_names
    assert "oidc:ops-team" in group_names


async def test_sso_login_generates_and_stores_nonce(
    aiohttp_client, router_app, sso_oidc_env,
):
    """Login mints a nonce, sends it to the IdP, and stashes it in the flow cookie.

    Nonce is the OIDC anti-replay / token-injection defence binding the
    id_token to this specific auth attempt. It MUST reach the authorize
    request AND be recoverable from the flow cookie so the callback can
    enforce it against the returned id_token.
    """
    import sys
    sso = sys.modules["agent_mcp.router.sso"]

    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    assert resp.status in (302, 303)
    qs = urllib.parse.parse_qs(
        urllib.parse.urlparse(resp.headers["Location"]).query
    )
    assert "nonce" in qs, "authorize request MUST carry a nonce"
    url_nonce = qs["nonce"][0]
    assert url_nonce, "nonce MUST be non-empty"

    flow_cookie = resp.cookies.get("agent_mcp_sso_flow")
    assert flow_cookie is not None
    flow = sso._decode_flow_cookie(flow_cookie.value)
    assert flow is not None
    assert flow.nonce == url_nonce, (
        "the nonce stored in the flow cookie MUST match the one sent to the IdP"
    )


async def test_sso_callback_passes_stored_nonce_to_decode(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """The callback threads the flow-cookie nonce into id_token validation.

    Without this the id_token is decoded with ``nonce=None`` and
    CodeIDToken.validate() cannot enforce the anti-replay binding.
    """
    import sys
    sso = sys.modules["agent_mcp.router.sso"]

    _patch_idp(monkeypatch, id_token_claims={
        "sub": "u", "email": "nonce@example.test",
        "preferred_username": "nonce", "groups": [],
    })

    captured: dict[str, Any] = {}

    def _capturing_decode(token, metadata, client_id, nonce=None):
        captured["nonce"] = nonce
        return {
            "sub": "u", "email": "nonce@example.test",
            "preferred_username": "nonce", "groups": [],
        }

    monkeypatch.setattr(sso, "_decode_id_token", _capturing_decode)

    client = await aiohttp_client(router_app)
    init = await client.get("/agent-mcp/sso/login", allow_redirects=False)
    flow_cookie = init.cookies.get("agent_mcp_sso_flow")
    assert flow_cookie is not None
    flow = sso._decode_flow_cookie(flow_cookie.value)
    assert flow is not None and flow.nonce
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(init.headers["Location"]).query
    )["state"][0]

    cb = await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "ok", "state": state},
        allow_redirects=False,
    )
    assert cb.status in (302, 303), await cb.text()
    assert captured.get("nonce") == flow.nonce, (
        "callback MUST pass the stored flow nonce into _decode_id_token"
    )


async def test_decode_id_token_rejects_nonce_mismatch(monkeypatch):
    """_decode_id_token enforces the nonce via CodeIDToken.validate().

    A signed id_token whose ``nonce`` claim doesn't match the expected
    value MUST be rejected; a matching one MUST pass. This exercises the
    real Authlib validation path (JWKS fetch patched, signature real).
    """
    import sys
    import time

    from authlib.jose import JsonWebKey, jwt
    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is None:
        import importlib
        sso = importlib.import_module("agent_mcp.router.sso")

    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    pub = key.as_dict(is_private=False)
    pub["kid"] = "k1"
    metadata = {
        "issuer": _FAKE_ISSUER,
        "jwks_uri": f"{_FAKE_ISSUER}/certs",
    }
    now = int(time.time())
    claims = {
        "iss": _FAKE_ISSUER, "aud": _FAKE_CLIENT_ID, "sub": "u",
        "exp": now + 300, "iat": now, "nonce": "the-real-nonce",
    }
    token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key).decode()

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": [pub]}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())

    # Matching nonce validates.
    out = sso._decode_id_token(
        token, metadata, _FAKE_CLIENT_ID, nonce="the-real-nonce",
    )
    assert out["sub"] == "u"

    # Mismatched nonce is rejected.
    with pytest.raises(JoseError):
        sso._decode_id_token(
            token, metadata, _FAKE_CLIENT_ID, nonce="attacker-nonce",
        )


async def test_sso_callback_rejects_bad_state(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """Mismatched ``state`` rejects the callback with 400."""
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "u", "email": "x@x", "preferred_username": "x", "groups": [],
    })
    client = await aiohttp_client(router_app)
    # Initiate so the flow cookie is set, then submit a WRONG state.
    await client.get("/agent-mcp/sso/login", allow_redirects=False)
    cb = await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "x", "state": "this-is-not-the-state"},
        allow_redirects=False,
    )
    assert cb.status == 400
    # No session cookie on a rejected callback.
    assert "agent_mcp_session" not in cb.headers.get("Set-Cookie", "")


# ── Account-linking security tests (SSO takeover / reconciliation) ──


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


def _passwordless_users():
    """All SSO-minted (password_hash IS NULL) users.

    Filters out the sentinel operator the router fixtures seed (which
    is password-backed) so the assertions below count only the rows
    the SSO flow itself created.
    """
    import sqlite3

    from agent_mcp.router import identity

    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM users WHERE password_hash IS NULL"
            )
        ]


async def test_sso_unverified_email_does_not_take_over_local_user(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """An IdP email with ``email_verified`` != True MUST NOT link to a
    pre-existing local account of the same email — that would be an
    account-takeover primitive (attacker sets a victim operator's email
    at an IdP that never verifies it and logs in AS the victim).

    The colliding local account is left untouched; the SSO login lands
    on a FRESH passwordless identity instead.
    """
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    victim_id = identity.create_user(
        username="victim-admin",
        password="correct horse battery",
        email="admin@corp.test",
    )
    # Make the victim a sysadmin so a successful takeover would be
    # maximally damaging — the assertion below proves it did NOT happen.
    import sqlite3
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.execute(
            "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
            (victim_id,),
        )
        conn.commit()

    # Attacker-controlled IdP asserts the victim's email but does NOT
    # (cannot) assert email_verified.
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "attacker-subject",
        "email": "admin@corp.test",
        "email_verified": False,
        "preferred_username": "attacker",
        "groups": [],
    })
    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client)
    assert cb.status in (302, 303), await cb.text()

    # The victim row is untouched — still password-backed, still
    # sysadmin, no SSO login stamped onto it.
    victim = identity.get_user_by_id(victim_id)
    assert victim is not None
    assert victim["password_hash"] is not None
    assert victim["last_login_at"] is None, (
        "victim account was logged into — takeover occurred"
    )

    # A distinct, fresh passwordless identity was created for the SSO
    # login — it is NOT the victim and is NOT a sysadmin.
    fresh_users = _passwordless_users()
    assert len(fresh_users) == 1, (
        f"expected one fresh SSO user, got {fresh_users!r}"
    )
    fresh = fresh_users[0]
    assert fresh["is_sysadmin"] == 0
    assert fresh["user_id"] != victim_id


async def test_sso_emailless_login_reconciles_to_single_user(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """Two OIDC logins for the same subject with NO email claim resolve
    to ONE user row (not a fresh ``name``/``name-2``/… every time).

    Reconciliation keys on the stable ``(iss, sub)`` subject, not the
    (absent) email — otherwise every login mints a new user and any
    grants attached to the previous row are orphaned.
    """
    from agent_mcp.router import group_resolver, identity

    _patch_idp(monkeypatch, id_token_claims={
        "sub": "stable-subject-42",
        "preferred_username": "eve",
        "groups": [],
        # no "email" claim at all
    })
    client = await aiohttp_client(router_app)

    cb1 = await _drive_callback(client)
    assert cb1.status in (302, 303), await cb1.text()
    after_first = _passwordless_users()
    assert len(after_first) == 1, after_first
    user_id = after_first[0]["user_id"]

    # Attach a grant to the freshly-minted user; it MUST survive the
    # next login (i.e. the next login returns the SAME row).
    import secrets
    import sqlite3
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        gid = secrets.token_hex(8)
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, 'persisted-grant', 0, datetime('now'))",
            (gid,),
        )
        conn.execute(
            "INSERT INTO group_membership "
            "(group_id, member_user_id, member_group_id, added_at) "
            "VALUES (?, ?, NULL, datetime('now'))",
            (gid, user_id),
        )
        conn.commit()

    cb2 = await _drive_callback(client)
    assert cb2.status in (302, 303), await cb2.text()

    after_second = _passwordless_users()
    assert len(after_second) == 1, (
        f"emailless re-login minted a duplicate user: {after_second!r}"
    )
    assert after_second[0]["user_id"] == user_id
    # The grant attached after the first login is still resolved.
    assert gid in group_resolver.resolve_user_groups(user_id)


async def test_sso_wildcard_group_does_not_bind_existing_privileged_group(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """A wildcard group claim whose slug collides with a pre-existing
    LOCALLY-MANAGED privileged group MUST NOT bind the user into that
    group (which would hand out its caps / sysadmin bit).

    Wildcard-provisioned groups are namespaced (``oidc:<slug>``) so the
    collision is impossible: the user joins a fresh ``oidc:admins``
    group, never the locally-managed ``admins`` sysadmin group.
    """
    import secrets
    import sqlite3

    from agent_mcp.router import group_resolver, identity

    identity.run_router_migrations_upgrade()
    # Locally-managed, sysadmin-flagged group named "admins".
    admins_gid = secrets.token_hex(8)
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, 'admins', 1, datetime('now'))",
            (admins_gid,),
        )
        conn.commit()

    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_GROUP_MAPPING",
        json.dumps({"*": ""}),
    )
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "wild",
        "email": "mallory@corp.test",
        "email_verified": True,
        "preferred_username": "mallory",
        "groups": ["admins"],
    })
    client = await aiohttp_client(router_app)
    cb = await _drive_callback(client)
    assert cb.status in (302, 303), await cb.text()

    user = identity.get_user_by_username("mallory")
    assert user is not None
    resolved = group_resolver.resolve_user_groups(user["user_id"])
    # The user MUST NOT be a member of the pre-existing privileged group
    assert admins_gid not in resolved
    # …and therefore MUST NOT have inherited its sysadmin bit.
    assert group_resolver.resolve_user_is_sysadmin(user["user_id"]) is False
    # The user landed in the namespaced JIT group instead.
    assert "oidc:admins" in _group_names_for(resolved)


# ── Fail-closed nonce (round-3 security finding AC-1) ───────────────
#
# The flow cookie is unauthenticated base64(JSON) — no HMAC/signature —
# so an attacker can craft one that OMITS the ``nonce`` field. Authlib's
# ``validate_nonce`` is gated on ``if nonce_value:``: an EMPTY expected
# nonce performs NO comparison, so an id_token minted for a DIFFERENT
# authorization request would be accepted. The fix treats an absent /
# empty nonce as an INVALID flow (fail closed).


async def test_decode_flow_cookie_rejects_missing_nonce():
    """A flow cookie without a ``nonce`` field decodes to None (invalid).

    Pre-fix this defaulted the nonce to "" and returned a usable
    _FlowState, silently disabling nonce validation downstream.

    (async only to match this module's asyncio-marked convention; the
    assertions themselves are synchronous.)
    """
    import sys

    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is None:
        import importlib
        sso = importlib.import_module("agent_mcp.router.sso")

    # Attacker-craftable cookie: valid state + verifier, NO nonce.
    forged = _b64url_nopad(
        json.dumps({"state": "s", "verifier": "v" * 43}).encode()
    )
    assert sso._decode_flow_cookie(forged) is None

    # Empty-string nonce is likewise rejected.
    empty = _b64url_nopad(
        json.dumps(
            {"state": "s", "verifier": "v" * 43, "nonce": ""}
        ).encode()
    )
    assert sso._decode_flow_cookie(empty) is None

    # A cookie WITH a real nonce still decodes normally.
    good = _b64url_nopad(
        json.dumps(
            {"state": "s", "verifier": "v" * 43, "nonce": "real-nonce"}
        ).encode()
    )
    flow = sso._decode_flow_cookie(good)
    assert flow is not None and flow.nonce == "real-nonce"


# ── N1 bypass #4: the flow cookie must decode through the shared
# sanitization seam, not a bare json.loads (security-arch-hardening-
# consolidated.md, N1 finding) ────────────────────────────────────────
#
# The flow cookie is unsigned base64(JSON) -- attacker-craftable, per
# the nonce tests above -- so it's an untrusted decode point like any
# other. N1's own discovery test (test_arch_enforced_sanitization.py)
# declared this one exempt/deferred rather than missed; this closes it.


async def test_decode_flow_cookie_strips_hidden_unicode():
    """A flow-cookie field carrying a hidden-format Unicode character
    (e.g. a zero-width space) must come back stripped, the same way
    every other untrusted decode point does via
    ``json_utils.decode_untrusted_body`` -- not silently preserved by a
    bare ``json.loads``.
    """
    import sys

    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is None:
        import importlib
        sso = importlib.import_module("agent_mcp.router.sso")

    zwsp = "\u200b"  # ZERO WIDTH SPACE, a Cf character
    poisoned = _b64url_nopad(
        json.dumps(
            {
                "state": f"s{zwsp}tate",
                "verifier": "v" * 43,
                "nonce": "real-nonce",
            }
        ).encode()
    )
    flow = sso._decode_flow_cookie(poisoned)
    assert flow is not None
    assert zwsp not in flow.state, (
        "the flow cookie must decode through the shared sanitization "
        "seam (json_utils.decode_untrusted_body), which strips hidden-"
        "format Unicode -- a bare json.loads leaves it untouched"
    )


async def test_sso_callback_rejects_nonceless_flow_cookie(
    aiohttp_client, router_app, sso_oidc_env, monkeypatch,
):
    """A callback presenting a nonce-less flow cookie is rejected (400).

    Even though state matches and the (patched) IdP returns a valid
    id_token, the missing nonce means anti-replay can't be enforced,
    so the flow MUST be rejected and no session minted.
    """
    _patch_idp(monkeypatch, id_token_claims={
        "sub": "attacker", "email": "evil@example.test",
        "preferred_username": "evil", "groups": [],
    })
    client = await aiohttp_client(router_app)
    # Forge a flow cookie with a matching state but no nonce field.
    forged = _b64url_nopad(
        json.dumps(
            {"state": "forged-state", "verifier": "v" * 43}
        ).encode()
    )
    cb = await client.get(
        "/agent-mcp/sso/callback",
        params={"code": "c", "state": "forged-state"},
        cookies={"agent_mcp_sso_flow": forged},
        allow_redirects=False,
    )
    assert cb.status == 400, await cb.text()
    assert "agent_mcp_session" not in cb.headers.get("Set-Cookie", "")
    # No user was JIT-created off the rejected flow.
    from agent_mcp.router import identity
    assert identity.get_user_by_username("evil") is None
