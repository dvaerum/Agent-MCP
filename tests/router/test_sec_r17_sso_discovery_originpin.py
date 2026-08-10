"""OBS-R17-SSO: harden trust in the OIDC discovery document.

When OIDC is configured, ``sso._fetch_oidc_metadata`` fetches the
issuer's ``/.well-known/openid-configuration`` and the flow then trusts
its ``jwks_uri`` / ``token_endpoint`` / ``authorization_endpoint``
verbatim. A hostile / compromised IdP — or a MITM against an ``http://``
issuer — could point those at internal hosts (SSRF-ish).

Two defences, per the operator decision (2026-08-10):

  1. **Origin-pin.** After fetching discovery, every trusted endpoint
     MUST share the configured issuer's scheme + host (+ port). Any
     cross-origin endpoint fails the flow.
  2. **Refuse ``http://`` issuer** unless ``AGENT_MCP_SSO_OIDC_ALLOW_INSECURE``
     is explicitly opted in (so production requires https, local-dev
     IdPs can still run over http).

These are unit-level tests: SSO is unconfigured on deploy, so no live
IdP / network is involved — the discovery doc + env are supplied
directly.
"""

from __future__ import annotations

import pytest

from agent_mcp.router import sso

_ISSUER = "https://idp.example.test"


def _discovery(**overrides: str) -> dict[str, str]:
    """A well-formed, same-origin discovery doc; override to make it hostile."""
    doc = {
        "issuer": _ISSUER,
        "authorization_endpoint": f"{_ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": f"{_ISSUER}/protocol/openid-connect/token",
        "jwks_uri": f"{_ISSUER}/protocol/openid-connect/certs",
    }
    doc.update(overrides)
    return doc


# ── Origin-pin: cross-origin endpoints are rejected ─────────────────


def test_discovery_jwks_uri_foreign_host_rejected() -> None:
    doc = _discovery(jwks_uri="https://evil.internal/certs")
    with pytest.raises(sso.SSOConfigError):
        sso._assert_discovery_same_origin(_ISSUER, doc)


def test_discovery_token_endpoint_foreign_host_rejected() -> None:
    doc = _discovery(token_endpoint="https://169.254.169.254/token")
    with pytest.raises(sso.SSOConfigError):
        sso._assert_discovery_same_origin(_ISSUER, doc)


def test_discovery_authorization_endpoint_foreign_host_rejected() -> None:
    doc = _discovery(authorization_endpoint="https://attacker.example/auth")
    with pytest.raises(sso.SSOConfigError):
        sso._assert_discovery_same_origin(_ISSUER, doc)


def test_discovery_foreign_scheme_rejected() -> None:
    # Same host, downgraded scheme is still a different origin.
    doc = _discovery(jwks_uri="http://idp.example.test/certs")
    with pytest.raises(sso.SSOConfigError):
        sso._assert_discovery_same_origin(_ISSUER, doc)


def test_discovery_foreign_port_rejected() -> None:
    doc = _discovery(token_endpoint="https://idp.example.test:9999/token")
    with pytest.raises(sso.SSOConfigError):
        sso._assert_discovery_same_origin(_ISSUER, doc)


def test_discovery_missing_endpoint_rejected() -> None:
    doc = _discovery()
    del doc["jwks_uri"]
    with pytest.raises(sso.SSOConfigError):
        sso._assert_discovery_same_origin(_ISSUER, doc)


# ── Origin-pin: same-origin discovery is accepted (happy path) ──────


def test_discovery_same_origin_endpoints_accepted() -> None:
    # Must not raise.
    sso._assert_discovery_same_origin(_ISSUER, _discovery())


def test_discovery_default_port_normalized_accepted() -> None:
    # Explicit :443 on an https endpoint is the SAME origin as the
    # port-less https issuer — the default port must be normalised.
    doc = _discovery(jwks_uri="https://idp.example.test:443/certs")
    sso._assert_discovery_same_origin(_ISSUER, doc)


# ── Origin-pin wired into the network fetch ─────────────────────────


def test_fetch_oidc_metadata_enforces_origin_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile discovery doc must be rejected by the fetch itself."""

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return _discovery(jwks_uri="https://evil.internal/certs")

    monkeypatch.setattr("requests.get", lambda *a, **kw: _Resp())
    with pytest.raises(sso.SSOConfigError):
        sso._fetch_oidc_metadata(_ISSUER)


def test_fetch_oidc_metadata_same_origin_returns_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return _discovery()

    monkeypatch.setattr("requests.get", lambda *a, **kw: _Resp())
    out = sso._fetch_oidc_metadata(_ISSUER)
    assert out["jwks_uri"] == f"{_ISSUER}/protocol/openid-connect/certs"


# ── Refuse http issuer unless opted in ──────────────────────────────


def _set_oidc_env(monkeypatch: pytest.MonkeyPatch, issuer: str) -> None:
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_ISSUER", issuer)
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_CLIENT_ID", "agent-mcp-rp")
    monkeypatch.delenv("AGENT_MCP_SSO_PROXY_HEADER", raising=False)
    monkeypatch.delenv("AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", raising=False)


def test_http_issuer_refused_without_optin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oidc_env(monkeypatch, "http://idp.example.test")
    monkeypatch.delenv("AGENT_MCP_SSO_OIDC_ALLOW_INSECURE", raising=False)
    with pytest.raises(sso.SSOConfigError):
        sso.load_sso_config()


def test_http_issuer_allowed_with_optin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oidc_env(monkeypatch, "http://idp.example.test")
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_ALLOW_INSECURE", "1")
    settings = sso.load_sso_config()
    assert settings.mode is sso.SSOMode.OIDC
    assert settings.oidc is not None
    assert settings.oidc.issuer == "http://idp.example.test"


def test_https_issuer_accepted_without_optin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oidc_env(monkeypatch, _ISSUER)
    monkeypatch.delenv("AGENT_MCP_SSO_OIDC_ALLOW_INSECURE", raising=False)
    settings = sso.load_sso_config()
    assert settings.mode is sso.SSOMode.OIDC


def test_non_http_scheme_issuer_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oidc_env(monkeypatch, "ftp://idp.example.test")
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_ALLOW_INSECURE", "1")
    with pytest.raises(sso.SSOConfigError):
        sso.load_sso_config()
