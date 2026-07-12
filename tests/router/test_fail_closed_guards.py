"""Fail-closed deployment-contract guards (internet-hardening).

Two config landmines:

  1. Single-tenant mode disables ALL operator-session auth. Safe only
     on a loopback/UDS bind. The startup guard refuses to build the
     app when single-tenant is paired with a non-loopback bind, unless
     the operator explicitly acknowledges an isolated bind.

  2. Secure-cookie enforcement. AGENT_MCP_REQUIRE_SECURE_COOKIES=1
     forces the Secure flag on the session cookie even over plain
     HTTP (fail-closed) so a non-secure session cookie is never issued.
"""

from __future__ import annotations

import pytest


# ── Single-tenant + bind-host guard ────────────────────────────────


def test_single_tenant_nonloopback_bind_refuses_to_start(
    router_module, monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError, match="single-tenant"):
        router_module.make_app(single_tenant_name="only")


def test_single_tenant_empty_host_refuses_to_start(
    router_module, monkeypatch,
) -> None:
    """R6-F1: a present-but-empty AGENT_MCP_ROUTER_HOST (the
    ``Environment=AGENT_MCP_ROUTER_HOST=`` / ``docker -e …=`` "bind all
    interfaces" idiom) binds 0.0.0.0+:: at runtime. Single-tenant must
    fail closed exactly as it does for an explicit 0.0.0.0 — the guard
    must NOT mis-classify "" as loopback."""
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "")
    with pytest.raises(RuntimeError, match="single-tenant"):
        router_module.make_app(single_tenant_name="only")


def test_single_tenant_whitespace_host_refuses_to_start(
    router_module, monkeypatch,
) -> None:
    """R6-F1: whitespace-only host collapses to "" (bind all) too."""
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "   ")
    with pytest.raises(RuntimeError, match="single-tenant"):
        router_module.make_app(single_tenant_name="only")


def test_multi_tenant_empty_host_starts(
    router_module, monkeypatch,
) -> None:
    """R6-F1: multi-tenant HAS the operator-session gate, so the
    documented "empty = bind all interfaces" idiom stays legal — the
    guard must not turn into a blanket refusal of empty-host binds."""
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "")
    app = router_module.make_app()  # multi-tenant (no single_tenant_name)
    assert app is not None


def test_single_tenant_loopback_bind_starts(
    router_module, monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "127.0.0.1")
    app = router_module.make_app(single_tenant_name="only")
    assert app is not None


def test_single_tenant_default_bind_starts(router_module) -> None:
    """No AGENT_MCP_ROUTER_HOST set → defaults to loopback → OK."""
    app = router_module.make_app(single_tenant_name="only")
    assert app is not None


def test_single_tenant_nonloopback_allowed_with_opt_out(
    router_module, monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_MCP_ALLOW_INSECURE_BIND", "1")
    app = router_module.make_app(single_tenant_name="only")
    assert app is not None


def test_multi_tenant_nonloopback_bind_starts(
    router_module, monkeypatch,
) -> None:
    """Multi-tenant enforces auth, so a non-loopback bind is fine."""
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "0.0.0.0")
    app = router_module.make_app()  # multi-tenant (no single_tenant_name)
    assert app is not None


def test_host_is_loopback_classification(router_module) -> None:
    is_lb = router_module._host_is_loopback
    assert is_lb("127.0.0.1") is True
    assert is_lb("::1") is True
    assert is_lb("localhost") is True
    # R6-F1: empty / whitespace-only host binds ALL interfaces, same as
    # 0.0.0.0 — NOT loopback.
    assert is_lb("") is False
    assert is_lb("   ") is False
    assert is_lb("/run/agent-mcp/router.sock") is True
    assert is_lb("0.0.0.0") is False
    assert is_lb("192.168.1.10") is False


def test_resolve_bind_host_collapses_empty(router_module, monkeypatch) -> None:
    """R6-F1: the SHARED resolver both the guard and the runtime
    entrypoints call collapses a present-but-empty / whitespace-only
    env value to "" — so the string classified == the string bound."""
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "")
    assert router_module._resolve_bind_host() == ""
    monkeypatch.setenv("AGENT_MCP_ROUTER_HOST", "   ")
    assert router_module._resolve_bind_host() == ""
    monkeypatch.delenv("AGENT_MCP_ROUTER_HOST", raising=False)
    assert router_module._resolve_bind_host() == "127.0.0.1"


# ── Secure-cookie fail-closed flag ─────────────────────────────────


def _mocked_request(proto: str | None = None):
    from aiohttp.test_utils import make_mocked_request

    headers = {"X-Forwarded-Proto": proto} if proto else {}
    return make_mocked_request("POST", "/agent-mcp/login", headers=headers)


def test_require_secure_cookies_forces_flag_on_http_login(monkeypatch) -> None:
    from agent_mcp.router import login

    # Without the flag, plain HTTP → not secure.
    monkeypatch.delenv("AGENT_MCP_REQUIRE_SECURE_COOKIES", raising=False)
    assert login.cookie_secure_flag(_mocked_request("http")) is False

    # With the flag, plain HTTP → forced secure (fail-closed).
    monkeypatch.setenv("AGENT_MCP_REQUIRE_SECURE_COOKIES", "1")
    assert login.cookie_secure_flag(_mocked_request("http")) is True


def test_require_secure_cookies_forces_flag_in_sso(monkeypatch) -> None:
    from agent_mcp.router import sso

    monkeypatch.delenv("AGENT_MCP_REQUIRE_SECURE_COOKIES", raising=False)
    assert sso._cookie_secure_flag(_mocked_request("http")) is False

    monkeypatch.setenv("AGENT_MCP_REQUIRE_SECURE_COOKIES", "1")
    assert sso._cookie_secure_flag(_mocked_request("http")) is True


@pytest.mark.asyncio
@pytest.mark.no_auth_seed_session
async def test_login_cookie_is_secure_when_flag_set(
    aiohttp_client, router_module, monkeypatch,
) -> None:
    """End-to-end: a successful login over http still gets Secure."""
    monkeypatch.setenv("AGENT_MCP_REQUIRE_SECURE_COOKIES", "1")
    app = router_module.make_app()

    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    identity.create_user(username="scuser", password="pw")

    client = await aiohttp_client(app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "scuser", "password": "pw"},
        headers={"X-Forwarded-Proto": "http"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "agent_mcp_session" in set_cookie
    assert "Secure" in set_cookie, (
        f"expected Secure flag when REQUIRE_SECURE_COOKIES set; "
        f"got {set_cookie!r}"
    )
