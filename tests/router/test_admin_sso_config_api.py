"""Admin SSO config endpoint tests (Phase 3 Wave 3).

The dashboard's System → SSO tab fetches
``GET /agent-mcp/api/router/sso/config`` to learn the active mode
and its operator-visible knobs. Two access cases matter:

  * sysadmin caller → 200 + the introspected config (with the
    client secret reported only as a presence boolean).
  * regular operator (non-sysadmin) → 403 with the standard
    sysadmin-gate error envelope.

The config-introspection endpoint itself is small; the gate is
inherited from ``perm_gates.require_capability("system.sso.configure")``
(Wave 9 PR 4 supersedes the prior ``require_sysadmin`` wrapper),
but we still exercise the wiring end-to-end so a future refactor
that misses the gate registration trips a red flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


_API_HEADERS = {"Accept": "application/vnd.agent-mcp.v1+json"}


@pytest.fixture
def oidc_env_minimal(
    router_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Configure OIDC env vars + patch discovery for hermetic tests."""
    secret_file = tmp_path / "oidc.secret"
    secret_file.write_text("supersecret")
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_CLIENT_ID", "rp-id")
    monkeypatch.setenv(
        "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", str(secret_file),
    )
    monkeypatch.setenv("AGENT_MCP_SSO_OIDC_PROVIDER_NAME", "Test IdP")
    import sys
    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is not None:
        sso._reset_cache_for_tests()
    return secret_file


async def test_sso_config_endpoint_admits_sysadmin(
    aiohttp_client, router_app, oidc_env_minimal,
):
    """Sysadmin (the sentinel operator) gets the OIDC config back."""
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/sso/config", headers=_API_HEADERS,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    cfg = body["config"]
    assert cfg["mode"] == "oidc"
    assert cfg["oidc"]["issuer"] == "https://idp.test"
    assert cfg["oidc"]["client_id"] == "rp-id"
    assert cfg["oidc"]["client_secret_present"] is True
    # Client secret value itself MUST NOT be serialised.
    assert "client_secret" not in cfg["oidc"]
    assert cfg["oidc"]["provider_name"] == "Test IdP"


@pytest.mark.no_auth_seed_session
async def test_sso_config_endpoint_rejects_non_sysadmin(
    aiohttp_client, router_app, oidc_env_minimal,
):
    """A logged-in non-sysadmin operator gets a 403.

    The conftest seeds the sentinel operator via the env-var bootstrap
    on app startup, which turns the FIRST user into the sysadmin.
    We boot the TestServer (which fires the startup hook) BEFORE
    inserting our non-sysadmin user so the sysadmin promotion lands
    on the sentinel and not on our intended-non-sysadmin row.
    """
    from agent_mcp.router import identity

    client = await aiohttp_client(router_app)
    # Now the users table has the sentinel (is_sysadmin=1) — every
    # subsequent ``create_user`` lands at is_sysadmin=0.
    identity.create_user(username="not_sysadmin", password="password1234")

    login = await client.post(
        "/agent-mcp/login",
        data={"username": "not_sysadmin", "password": "password1234"},
        allow_redirects=False,
    )
    assert login.status == 303, await login.text()

    resp = await client.get(
        "/agent-mcp/api/router/sso/config", headers=_API_HEADERS,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    # Wave 9 PR 4: the gate is now require_capability("system.sso.configure"),
    # so the envelope's ``message`` names the missing cap rather than
    # the prior "is not a sysadmin" phrasing. The status code +
    # ``error`` discriminator are unchanged (the dashboard's ApiClient
    # keys off those).
    assert body["error"] == "forbidden"
    assert "system.sso.configure" in body["message"]


async def test_sso_config_endpoint_reports_builtin_when_off(
    aiohttp_client, router_app, monkeypatch, router_env,
):
    """When no SSO env is set, the endpoint reports mode=builtin."""
    for name in (
        "AGENT_MCP_SSO_OIDC_ISSUER",
        "AGENT_MCP_SSO_OIDC_CLIENT_ID",
        "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE",
        "AGENT_MCP_SSO_PROXY_HEADER",
    ):
        monkeypatch.delenv(name, raising=False)
    import sys
    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is not None:
        sso._reset_cache_for_tests()

    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/sso/config", headers=_API_HEADERS,
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["config"]["mode"] == "builtin"
    assert "oidc" not in body["config"]
    assert "proxy" not in body["config"]
