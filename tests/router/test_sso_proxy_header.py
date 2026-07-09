"""Proxy-header trust SSO tests (Phase 3 Wave 3 of prancy-napping-pie).

The router gains a second SSO mode: trust a header that an upstream
proxy (nginx + oauth2-proxy, traefik + forward-auth, tailscale-funnel
+ Tailnet identity, …) populates with the authenticated username. The
router treats that header as a session-equivalent identity and JIT-
creates the user if missing — same algorithm as the OIDC callback.

Env vars:

  AGENT_MCP_SSO_PROXY_HEADER                — header name, e.g.
                                              "Remote-User". Setting
                                              this turns the mode on.
  AGENT_MCP_SSO_PROXY_TRUSTED_IPS           — comma-separated list of
                                              source IPs allowed to
                                              send the trusted header
                                              (default: 127.0.0.1, ::1).
                                              Anything outside this set
                                              is treated as a forgery
                                              attempt — the header is
                                              silently dropped.
  AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN      — "true" / "false" — the
                                              ``is_sysadmin`` bit on
                                              JIT-created users. Default
                                              "false".

Critical safety property: the router MUST refuse to honour the header
when the request didn't originate from a trusted source. Otherwise a
remote attacker could spoof ``Remote-User: dennis`` and walk straight
in. The test below confirms this by hitting a project-scoped URL from
a simulated untrusted source.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sso_proxy_env(router_env, monkeypatch: pytest.MonkeyPatch):
    """Turn on proxy-header trust with localhost as the only trusted IP."""
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_HEADER", "Remote-User")
    monkeypatch.setenv(
        "AGENT_MCP_SSO_PROXY_TRUSTED_IPS", "127.0.0.1,::1",
    )
    monkeypatch.setenv(
        "AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN", "false",
    )


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.no_auth_seed_session
async def test_trusted_header_from_localhost_admits(
    aiohttp_client, router_app, sso_proxy_env, register_project,
):
    """A request from 127.0.0.1 with the trusted header is honoured."""
    register_project("proj-a")

    # router_module's autouse-style fixture seeded the sentinel
    # operator account, but the proxy-header path should NOT need
    # it — the JIT user is created on the fly. We do NOT pre-login.
    client = await aiohttp_client(router_app)
    # The TestServer binds 127.0.0.1, so the header IS coming from a
    # trusted source by construction.
    #
    # Probe endpoint: ``/api/router/projects`` (auth-only ``gated``, no
    # capability gate) rather than ``/api/router/users``. The latter is
    # now capability-gated (``system.users.manage``) by the
    # viewer-read-gating fix (2026-07-08, finding 2), so a JIT-created
    # non-sysadmin proxy user would 403 there — which would test the
    # authz gate, not the proxy-header AUTH gate this test is about. A
    # bare-authenticated user reads ``/projects`` with 200, so a 200
    # here isolates "the proxy-header gate admitted the request".
    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={
            "Remote-User": "alice-from-proxy",
            "Accept": "application/vnd.agent-mcp.v1+json",
        },
    )
    # 200 means the proxy-header gate let us through.
    assert resp.status == 200, await resp.text()

    # The JIT user MUST exist after a successful proxy-trusted request.
    from agent_mcp.router import identity
    row = identity.get_user_by_username("alice-from-proxy")
    assert row is not None
    assert row["password_hash"] is None


@pytest.mark.no_auth_seed_session
async def test_trusted_header_from_untrusted_source_rejected(
    aiohttp_client, router_app, monkeypatch, router_env, register_project,
):
    """Untrusted source IP MUST NOT have its Remote-User honoured."""
    # Pin the trusted IP list to something the TestServer ISN'T using
    # (the server binds 127.0.0.1). The header should be silently
    # ignored, falling through to the no-session 401.
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_HEADER", "Remote-User")
    monkeypatch.setenv(
        "AGENT_MCP_SSO_PROXY_TRUSTED_IPS", "10.99.99.99",
    )
    register_project("proj-b")
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/users",
        headers={
            "Remote-User": "attacker",
            "Accept": "application/vnd.agent-mcp.v1+json",
        },
    )
    assert resp.status == 401, await resp.text()
    # The forged identity MUST NOT have been created.
    from agent_mcp.router import identity
    assert identity.get_user_by_username("attacker") is None


@pytest.mark.no_auth_seed_session
async def test_default_sysadmin_flag_applied_on_jit_create(
    aiohttp_client, router_app, router_env, monkeypatch, register_project,
):
    """``AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN=true`` flips the bit on JIT."""
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_HEADER", "Remote-User")
    monkeypatch.setenv(
        "AGENT_MCP_SSO_PROXY_TRUSTED_IPS", "127.0.0.1,::1",
    )
    monkeypatch.setenv(
        "AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN", "true",
    )
    register_project("proj-c")
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/users",
        headers={
            "Remote-User": "first-proxy-admin",
            "Accept": "application/vnd.agent-mcp.v1+json",
        },
    )
    assert resp.status == 200, await resp.text()
    from agent_mcp.router import identity
    row = identity.get_user_by_username("first-proxy-admin")
    assert row is not None
    assert row["is_sysadmin"] == 1


@pytest.mark.no_auth_seed_session
async def test_repeated_proxy_header_reconciles_to_single_user(
    aiohttp_client, router_app, sso_proxy_env, register_project,
):
    """Two requests carrying the SAME trusted ``Remote-User`` MUST
    resolve to ONE user row — not a fresh ``alice``/``alice-2``/… per
    request (which would leak unbounded rows and orphan any grants).

    The proxy path re-runs on every request (no session cookie in
    proxy mode), so stable reconciliation by the trusted username is
    the property under test.
    """
    register_project("proj-recon")
    client = await aiohttp_client(router_app)

    async def _probe():
        return await client.get(
            "/agent-mcp/api/router/projects",
            headers={
                "Remote-User": "carol-from-proxy",
                "Accept": "application/vnd.agent-mcp.v1+json",
            },
        )

    r1 = await _probe()
    assert r1.status == 200, await r1.text()

    from agent_mcp.router import identity
    import sqlite3
    import secrets

    def _proxy_users():
        with sqlite3.connect(str(identity.get_router_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM users WHERE username LIKE 'carol-from-proxy%'"
                )
            ]

    first = _proxy_users()
    assert len(first) == 1, first
    user_id = first[0]["user_id"]

    # Attach a grant; it MUST survive the second request.
    from agent_mcp.router import group_resolver
    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        gid = secrets.token_hex(8)
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, 'proxy-grant', 0, datetime('now'))",
            (gid,),
        )
        conn.execute(
            "INSERT INTO group_membership "
            "(group_id, member_user_id, member_group_id, added_at) "
            "VALUES (?, ?, NULL, datetime('now'))",
            (gid, user_id),
        )
        conn.commit()

    r2 = await _probe()
    assert r2.status == 200, await r2.text()

    second = _proxy_users()
    assert len(second) == 1, (
        f"repeated proxy-header request minted a duplicate user: {second!r}"
    )
    assert second[0]["user_id"] == user_id
    assert gid in group_resolver.resolve_user_groups(user_id)


def _users_by_subject_prefix(prefix: str) -> list[dict]:
    """All users whose ``sso_subject`` begins with ``prefix``."""
    from agent_mcp.router import identity
    import sqlite3

    with sqlite3.connect(str(identity.get_router_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM users WHERE sso_subject LIKE ?",
                (prefix + "%",),
            )
        ]


@pytest.mark.no_auth_seed_session
async def test_sanitise_colliding_usernames_map_to_distinct_users(
    aiohttp_client, router_app, sso_proxy_env, register_project,
):
    """Two DISTINCT trusted principals whose usernames sanitise-COLLIDE
    (``a.b@corp`` and ``a-b@corp`` both slugify to ``a-b-corp``) MUST
    resolve to TWO distinct user rows — not reconcile the second INTO
    the first's account (a login-as regression: the second principal
    would inherit the first's groups/grants/sysadmin bit).

    The proxy subject key MUST derive from the RAW trusted username so
    distinct upstream identities stay distinct; sanitisation is only for
    the display username.
    """
    register_project("proj-collide")
    client = await aiohttp_client(router_app)

    async def _probe(remote_user: str):
        return await client.get(
            "/agent-mcp/api/router/projects",
            headers={
                "Remote-User": remote_user,
                "Accept": "application/vnd.agent-mcp.v1+json",
            },
        )

    r1 = await _probe("a.b@corp")
    assert r1.status == 200, await r1.text()
    r2 = await _probe("a-b@corp")
    assert r2.status == 200, await r2.text()

    rows = _users_by_subject_prefix("proxy:")
    subjects = {r["sso_subject"] for r in rows}
    user_ids = {r["user_id"] for r in rows}
    assert len(user_ids) == 2, (
        f"sanitise-colliding principals collapsed into one account: {rows!r}"
    )
    # Distinct subjects keyed on the raw (un-sanitised) username.
    assert subjects == {"proxy:a.b@corp", "proxy:a-b@corp"}, subjects


@pytest.mark.no_seed_operator
async def test_proxy_empty_table_default_sysadmin_false_forces_setup(
    aiohttp_client, router_app, router_env, monkeypatch, register_project,
):
    """On an EMPTY users table with ``DEFAULT_SYSADMIN=false`` the proxy
    path MUST NOT auto-mint a bootstrap sysadmin. The operator explicitly
    declined proxy auto-sysadmin, so the first admin must be minted via
    the setup wizard. The JIT MUST NOT fire (which would both violate the
    flag AND make the users table non-empty, locking the wizard away).
    """
    from agent_mcp.router import identity
    identity.run_router_migrations_upgrade()

    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_HEADER", "Remote-User")
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_TRUSTED_IPS", "127.0.0.1,::1")
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN", "false")

    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={
            "Remote-User": "should-not-be-admin",
            "Accept": "application/vnd.agent-mcp.v1+json",
        },
    )
    # No identity minted → the request is unauthenticated.
    assert resp.status == 401, await resp.text()
    # The users table is still empty — the operator can still reach the
    # setup wizard to bootstrap a real admin.
    assert identity.get_user_by_username("should-not-be-admin") is None
    from agent_mcp.router.setup_wizard import users_table_is_empty
    assert users_table_is_empty() is True


@pytest.mark.no_seed_operator
async def test_proxy_empty_table_default_sysadmin_true_still_bootstraps(
    aiohttp_client, router_app, router_env, monkeypatch, register_project,
):
    """``DEFAULT_SYSADMIN=true`` bootstrap is preserved: the first proxy
    login on an empty table DOES mint a sysadmin (avoids lock-out for
    deployments that boot straight into proxy-header SSO)."""
    from agent_mcp.router import identity
    identity.run_router_migrations_upgrade()

    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_HEADER", "Remote-User")
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_TRUSTED_IPS", "127.0.0.1,::1")
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN", "true")

    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={
            "Remote-User": "boot-admin",
            "Accept": "application/vnd.agent-mcp.v1+json",
        },
    )
    assert resp.status == 200, await resp.text()
    row = identity.get_user_by_username("boot-admin")
    assert row is not None
    assert row["is_sysadmin"] == 1
