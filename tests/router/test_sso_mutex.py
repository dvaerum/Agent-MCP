"""SSO config mutex tests (Phase 3 Wave 3 of prancy-napping-pie).

OIDC and proxy-header trust are the only two SSO modes the router
supports; they are mutually exclusive by design. Allowing both at
once would create surprising precedence rules (does the cookie win
over the header? does an active OIDC session shadow a header from a
trusted proxy?) and a security footgun (the operator may believe one
is the "real" auth path while the other quietly admits requests).

Failure mode: ``make_app`` (and the equivalent CLI startup path)
raises a clean ``SSOConfigError`` with the conflicting env-var names
in the message. The router's main() converts that into a non-zero
exit before any port is bound.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def test_both_modes_configured_is_startup_error(
    router_env, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    """Setting both OIDC issuer + proxy header raises at config load."""
    secret = tmp_path / "s"
    secret.write_text("x")
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_CLIENT_ID", "c")
    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", str(secret),
    )
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_HEADER", "Remote-User")

    # Drop any stale import so the env vars get re-read.
    for mod in (
        "agent_mcp.router.sso",
    ):
        sys.modules.pop(mod, None)
    sso = importlib.import_module("agent_mcp.router.sso")

    with pytest.raises(sso.SSOConfigError) as excinfo:
        sso.load_sso_config()
    msg = str(excinfo.value).lower()
    assert "oidc" in msg and "proxy" in msg, msg


def test_only_oidc_loads_clean(
    router_env, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    """OIDC alone produces a config in OIDC mode."""
    secret = tmp_path / "s"
    secret.write_text("the-secret")
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_CLIENT_ID", "c")
    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", str(secret),
    )
    monkeypatch.delenv("AGENT_MCP_SSO_PROXY_HEADER", raising=False)

    for mod in ("agent_mcp.router.sso",):
        sys.modules.pop(mod, None)
    sso = importlib.import_module("agent_mcp.router.sso")
    cfg = sso.load_sso_config()
    assert cfg.mode == sso.SSOMode.OIDC
    assert cfg.oidc is not None
    assert cfg.oidc.client_id == "c"
    assert cfg.oidc.client_secret == "the-secret"


def test_only_proxy_loads_clean(
    router_env, monkeypatch: pytest.MonkeyPatch,
):
    """Proxy-header alone produces a config in PROXY_HEADER mode."""
    monkeypatch.delenv("AGENT_MCP_SSO_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("AGENT_MCP_SSO_PROXY_HEADER", "Remote-User")
    monkeypatch.setenv(
        "AGENT_MCP_SSO_PROXY_TRUSTED_IPS", "127.0.0.1,::1",
    )

    for mod in ("agent_mcp.router.sso",):
        sys.modules.pop(mod, None)
    sso = importlib.import_module("agent_mcp.router.sso")
    cfg = sso.load_sso_config()
    assert cfg.mode == sso.SSOMode.PROXY_HEADER
    assert cfg.proxy is not None
    assert cfg.proxy.trust_header == "Remote-User"
    assert "127.0.0.1" in cfg.proxy.trusted_ips


def test_neither_set_defaults_builtin(
    router_env, monkeypatch: pytest.MonkeyPatch,
):
    """No SSO env vars → BUILTIN mode (the legacy username/password path)."""
    for name in (
        "AGENT_MCP_SSO_OIDC_ISSUER",
        "AGENT_MCP_SSO_OIDC_CLIENT_ID",
        "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE",
        "AGENT_MCP_SSO_PROXY_HEADER",
    ):
        monkeypatch.delenv(name, raising=False)
    for mod in ("agent_mcp.router.sso",):
        sys.modules.pop(mod, None)
    sso = importlib.import_module("agent_mcp.router.sso")
    cfg = sso.load_sso_config()
    assert cfg.mode == sso.SSOMode.BUILTIN
    assert cfg.oidc is None
    assert cfg.proxy is None
