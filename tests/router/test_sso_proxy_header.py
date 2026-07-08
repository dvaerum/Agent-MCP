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
