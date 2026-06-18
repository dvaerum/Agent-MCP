"""REST resource shape for project lifecycle (ADR 0014).

  Legacy URL                          →  REST resource (ADR 0014)
  ────────────────────────────────────────────────────────────────────
  POST /__create               name=…   →  POST   /api/router/projects        {"name": "…"}
  POST /__unregister           name=…   →  DELETE /api/router/projects/<name>  [?delete_workspace=true]
  POST /__rename old=…&new=…&grace=…    →  PATCH  /api/router/projects/<name>  {"name": "…", "grace_days": N}
  POST /__stop                 name=…   →  POST   /api/router/projects/<name>/stop
  GET  /__alias-usage?alias=…           →  GET    /api/router/projects/<name>/aliases?alias=<a>
  POST /__remove-alias       alias=…    →  DELETE /api/router/projects/<name>/aliases/<alias>

All endpoints:
  - Strictly Accept-header gated (PR-A): the v1 media type is required.
  - JSON request bodies (no form-encoded data).
  - JSON responses (no 303 redirects to the index page).
  - One unified error envelope (audit §2.5):
      {"success": false, "error": "<code>", "message": "<human>"}
  - Success responses use the same envelope with success: true plus
    the resource-specific fields.

The legacy ``/__*`` URLs were retired in ADR 0014; the
``test_router_admin_api`` module guards that retirement.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── POST /api/router/projects — create ─────────────────────────────────────


async def test_create_project_via_json_post_returns_resource(
    aiohttp_client, router_app,
) -> None:
    """``POST /api/router/projects`` with ``{"name": "<slug>"}`` JSON body
    creates the project. Response is JSON; no 303 redirect."""
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
    assert "workspace" in body["project"]


async def test_create_project_rejects_invalid_slug(
    aiohttp_client, router_app,
) -> None:
    """Invalid slug → 400 with the unified error envelope."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "BAD-Slug"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "invalid_name"
    assert "message" in body
    # Error envelope must surface the slug regex requirement so the
    # caller knows how to fix the request without reading docs.
    assert "lowercase" in body["message"].lower()


async def test_create_project_rejects_reserved_name(
    aiohttp_client, router_app,
) -> None:
    """Reserved names (api, app, assets, mcp from PR-B audit §2.6)
    surface as a structured error, not a bare HTTP reason string."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "api"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "invalid_name"
    assert "reserved" in body["message"].lower()


async def test_create_project_rejects_duplicate(
    aiohttp_client, router_app, register_project,
) -> None:
    """409 with discriminator ``error == "already_registered"``."""
    register_project("taken")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "taken"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 409
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "already_registered"


async def test_create_project_requires_strict_accept_header(
    aiohttp_client, router_app,
) -> None:
    """The PR-A gate applies — no v1 Accept, no entry."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "x"}),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 406


# ── DELETE /api/router/projects/<name> — unregister ────────────────────────


async def test_delete_project_returns_unregister_envelope(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("doomed")
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/doomed",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["success"] is True
    assert body["unregistered"] == "doomed"
    assert body["workspace_deleted"] is False


async def test_delete_project_with_workspace_query_flag(
    aiohttp_client, router_app, register_project, router_env,
) -> None:
    """``?delete_workspace=true`` cascades into the on-disk dir.
    Query-string signal (audit §3.2 — pick ONE convention, query
    string preferred for DELETE since browsers strip DELETE bodies
    on some Fetch implementations).

    Defence-in-depth: the cascade is only honoured when the workspace
    resolves inside the configured DEFAULT_WORKSPACE_PARENT (see
    ``_is_within_default_workspace``). The test fixture configures
    DEFAULT_WORKSPACE to ``env.root / "workspaces"`` so we put the
    workspace there to actually exercise the delete path."""
    ws = router_env.root / "workspaces" / "doomed"
    ws.mkdir(parents=True)
    register_project("doomed", str(ws))
    assert ws.exists()
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/doomed?delete_workspace=true",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["success"] is True
    assert body["workspace_deleted"] is True
    assert not ws.exists()


async def test_delete_unknown_project_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/never-existed",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_registered"


# ── PATCH /api/router/projects/<name> — rename ────────────────────────────────


async def test_rename_project_via_json_body(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("oldname")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/oldname",
        data=json.dumps({"name": "newname", "grace_days": 7}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["success"] is True
    assert body["renamed"]["from"] == "oldname"
    assert body["renamed"]["to"] == "newname"
    assert body["alias"]["name"] == "oldname"
    assert body["alias"]["grace_days"] == 7


async def test_rename_unknown_project_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/ghost",
        data=json.dumps({"name": "irrelevant"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404
    body = await resp.json()
    assert body["error"] == "not_registered"


async def test_rename_with_invalid_new_name_400(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("a")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/a",
        data=json.dumps({"name": "_invalid"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_name"


# ── POST /api/router/projects/<name>/stop ──────────────────────────────────


async def test_stop_project_returns_envelope(
    aiohttp_client, router_app, register_project,
) -> None:
    """Stop with no active backend → 200 success envelope ('was already
    stopped' is success; the operation is idempotent)."""
    register_project("idle")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects/idle/stop",
        data="{}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["success"] is True
    assert body["stopped"] == "idle"


# Alias REST endpoints
# (GET/DELETE /api/router/projects/<name>/aliases/<alias>) are
# covered by ``test_alias_management.py``. ADR 0014 brought them
# in as siblings to the rest of the admin surface.
