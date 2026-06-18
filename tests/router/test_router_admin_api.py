"""Router admin REST surface — retires the legacy ``__`` URL namespace.

URL redesign (plan: docs/adr/0014-rest-admin-api.md). All operator-
facing endpoints that used to live at ``/agent-mcp/__*`` are now
REST-shaped under ``/agent-mcp/api/router/...``. Auth flows through
the PR D operator-session middleware automatically because the new
paths live under ``/api/...``; a single public service descriptor
sits at ``/api/router/health``.

URL map (legacy → new):

    GET    /__projects                          → GET    /api/router/projects
    POST   /__create  (form-encoded)            → POST   /api/router/projects  (JSON)
    POST   /__rename                            → PATCH  /api/router/projects/<name>
    POST   /__unregister                        → DELETE /api/router/projects/<name>
    POST   /__stop                              → POST   /api/router/projects/<name>/stop
    GET    /__overview                          → GET    /api/router/overview
    GET    /__client-config/<n>.mcp.json        → GET    /api/router/projects/<name>/client-config
    GET    /__client-installer/<n>.sh           → GET    /api/router/projects/<name>/installer
    GET    /__alias-usage                       → GET    /api/router/projects/<name>/aliases
    POST   /__remove-alias                      → DELETE /api/router/projects/<name>/aliases/<alias>
    POST   /__create-agent                      → POST   /api/router/projects/<name>/agents
    (new)                                       → GET    /api/router/health  (public)

Each section has a positive test (new URL + method + body returns 2xx
with a valid session cookie) and a negative test (legacy ``__`` URL
returns 404 — gone, not behind 401, not 410).
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── GET /api/router/health (public, no auth) ────────────────────────


@pytest.mark.no_auth_seed_session
async def test_health_descriptor_is_public(
    aiohttp_client, router_app,
) -> None:
    """``GET /api/router/health`` is the one new public endpoint —
    a JSON service descriptor reachable without a session cookie.
    Other admin routes require auth; this one's the unauthenticated
    'is the router up' probe."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/health", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("ok") is True
    assert "version" in body


# ── GET /api/router/projects — list projects ────────────────────────


async def test_list_projects_returns_json(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert sorted(body["projects"]) == ["alpha", "beta"]


async def test_legacy_projects_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__projects")

    assert resp.status == 404


# ── POST /api/router/projects — create ──────────────────────────────


async def test_create_project_via_json_post(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "freshly-minted"}),
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["project"]["name"] == "freshly-minted"


async def test_legacy_create_url_returns_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__create",
        data={"name": "doomed"},
        allow_redirects=False,
    )

    assert resp.status == 404


# ── PATCH /api/router/projects/<name> — rename ──────────────────────


async def test_rename_project_via_patch(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("old-name")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/old-name",
        data=json.dumps({"name": "new-name", "grace_days": 7}),
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["renamed"] == {"from": "old-name", "to": "new-name"}
    assert body["alias"]["name"] == "old-name"


async def test_legacy_rename_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("old-name")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__rename",
        data={"old_name": "old-name", "new_name": "new-name"},
    )

    assert resp.status == 404


# ── DELETE /api/router/projects/<name> — unregister ─────────────────


async def test_delete_project_via_delete(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("doomed")
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/doomed",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["unregistered"] == "doomed"


async def test_legacy_unregister_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("doomed")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__unregister", data={"name": "doomed"},
    )

    assert resp.status == 404


# ── POST /api/router/projects/<name>/stop ───────────────────────────


async def test_stop_project_returns_envelope(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("sleepy")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects/sleepy/stop",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["stopped"] == "sleepy"


async def test_legacy_stop_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("sleepy")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__stop", data={"name": "sleepy"},
    )

    assert resp.status == 404


# ── GET /api/router/overview ────────────────────────────────────────


async def test_overview_returns_envelope(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/overview", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert "projects" in body
    assert any(p["name"] == "alpha" for p in body["projects"])


async def test_legacy_overview_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__overview")

    assert resp.status == 404


# ── GET /api/router/projects/<name>/client-config ───────────────────


async def test_client_config_returns_mcp_json(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("alpha")
    router_module._agent_token_cache["alpha"] = (
        9.9e18, {"tok-admin": "Admin"},
    )
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects/alpha/client-config",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    # New Content-Type advertises the .mcp.json shape via a vendor media
    # type; the body remains a valid .mcp.json document.
    ctype = resp.headers.get("Content-Type", "")
    assert "agent-mcp.client-config" in ctype, (
        f"expected vendor media type, got {ctype!r}"
    )
    body = await resp.json()
    assert "mcpServers" in body


async def test_legacy_client_config_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__client-config/alpha.mcp.json")

    assert resp.status == 404


# ── GET /api/router/projects/<name>/installer ───────────────────────


async def test_installer_returns_shell_script(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("alpha")
    router_module._agent_token_cache["alpha"] = (
        9.9e18, {"tok-admin": "Admin"},
    )
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects/alpha/installer",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    ctype = resp.headers.get("Content-Type", "")
    assert "text/x-shellscript" in ctype, (
        f"expected text/x-shellscript, got {ctype!r}"
    )


async def test_legacy_installer_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__client-installer/alpha.sh")

    assert resp.status == 404


# ── GET /api/router/projects/<name>/aliases ─────────────────────────


async def test_alias_usage_lookup(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """After a rename, the old name is parked as an alias on the new
    project. The router-admin alias usage endpoint resolves the alias
    against the project and returns the (project, expiry, agents) shape."""
    register_project("alpha")
    # Rename to create an alias entry: alpha → alpha-renamed, with the
    # alias `alpha` pointing at `alpha-renamed`.
    router_module._REGISTRY.rename("alpha", "alpha-renamed", grace_days=30)
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects/alpha-renamed/aliases?alias=alpha",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["alias"] == "alpha"
    assert body["project"] == "alpha-renamed"
    assert isinstance(body["agents"], list)


async def test_legacy_alias_usage_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__alias-usage?alias=somename",
    )

    assert resp.status == 404


# ── DELETE /api/router/projects/<name>/aliases/<alias> ──────────────


async def test_remove_alias_via_delete(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("alpha")
    router_module._REGISTRY.rename("alpha", "alpha-renamed", grace_days=30)
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/alpha-renamed/aliases/alpha",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["removed"] == "alpha"
    assert body["project"] == "alpha-renamed"


async def test_legacy_remove_alias_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__remove-alias",
        data={"name": "alpha", "alias": "somename"},
    )

    assert resp.status == 404


# ── POST /api/router/projects/<name>/agents — admin create-agent ────


async def test_legacy_create_agent_url_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    """The router-admin create-agent endpoint (a wrapper that proxies
    via _mcp_call_admin to seed a bootstrap task) moves to the new URL.
    The legacy ``__create-agent`` shape is gone."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__create-agent",
        data={"name": "alpha", "agent_id": "worker-1"},
    )

    assert resp.status == 404


# ── Reserved-name reservation: "router" rejects ─────────────────────


async def test_reserved_name_router_is_rejected(router_module) -> None:
    """A project literally named ``router`` would collide with the new
    ``/api/router/...`` admin namespace. The slug validator MUST reject
    it at create / rename time."""
    err = router_module._validate_name("router", existing={})
    assert err is not None
    assert "reserved" in err.lower()


# ── Auth gate: admin routes require a session cookie ────────────────


@pytest.mark.no_auth_seed_session
async def test_unauth_call_to_router_admin_returns_401(
    aiohttp_client, router_app, register_project,
) -> None:
    """Without a session cookie, ``GET /api/router/projects`` is gated
    by ``require_operator_session_middleware`` (PR D). The only public
    admin endpoint is ``/api/router/health``."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 401
