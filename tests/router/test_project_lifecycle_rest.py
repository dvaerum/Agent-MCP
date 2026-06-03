"""PR-C — REST resource shape for project lifecycle.

Locked design (from /grill-me, PR plan):

  Form-encoded                          →  JSON-bodied REST resource
  ────────────────────────────────────────────────────────────────────
  POST /__create               name=…   →  POST   /api/projects        {"name": "…"}
  POST /__unregister           name=…   →  DELETE /api/projects/<name>  [?delete_workspace=true]
  POST /__rename old=…&new=…&grace=…    →  POST   /api/projects/<name>/rename {"new_name": "…", "grace_days": N}
  POST /__stop                 name=…   →  POST   /api/projects/<name>/stop
  GET  /__alias-usage?alias=…           →  GET    /api/projects/<name>/aliases/<alias>
  POST /__remove-alias       alias=…    →  DELETE /api/projects/<name>/aliases/<alias>

All new endpoints:
  - Strictly Accept-header gated (PR-A): the v1 media type is required.
  - JSON request bodies (no form-encoded data).
  - JSON responses (no 303 redirects to the index page).
  - One unified error envelope (audit §2.5 — picks the
    _dispatch_through_tool shape since it already has the most
    adoption):
      {"success": false, "error": "<code>", "message": "<human>", "code": "<http-status>"}
  - Success responses use the same envelope with success: true plus
    the resource-specific fields.

Old endpoints are kept as-is (still form-encoded, still 303-redirect)
so the dashboard's pre-PR-C modals keep working during the migration
window. They're tagged DEPRECATED in comments; PR-F or a later major
removes them.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── POST /api/projects — create ─────────────────────────────────────


async def test_create_project_via_json_post_returns_resource(
    aiohttp_client, router_app,
) -> None:
    """``POST /api/projects`` with ``{"name": "<slug>"}`` JSON body
    creates the project. Response is JSON; no 303 redirect."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/projects",
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
        "/agent-mcp/api/projects",
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
        "/agent-mcp/api/projects",
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
        "/agent-mcp/api/projects",
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
        "/agent-mcp/api/projects",
        data=json.dumps({"name": "x"}),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 406


# ── DELETE /api/projects/<name> — unregister ────────────────────────


async def test_delete_project_returns_unregister_envelope(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("doomed")
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/projects/doomed",
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
        "/agent-mcp/api/projects/doomed?delete_workspace=true",
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
        "/agent-mcp/api/projects/never-existed",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_registered"


# ── POST /api/projects/<name>/rename ────────────────────────────────


async def test_rename_project_via_json_body(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("oldname")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/projects/oldname/rename",
        data=json.dumps({"new_name": "newname", "grace_days": 7}),
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

    resp = await client.post(
        "/agent-mcp/api/projects/ghost/rename",
        data=json.dumps({"new_name": "irrelevant"}),
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

    resp = await client.post(
        "/agent-mcp/api/projects/a/rename",
        data=json.dumps({"new_name": "_invalid"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_name"


# ── POST /api/projects/<name>/stop ──────────────────────────────────


async def test_stop_project_returns_envelope(
    aiohttp_client, router_app, register_project,
) -> None:
    """Stop with no active backend → 200 success envelope ('was already
    stopped' is success; the operation is idempotent)."""
    register_project("idle")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/projects/idle/stop",
        data="{}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["success"] is True
    assert body["stopped"] == "idle"


# ── Alias resources ────────────────────────────────────────────────
#
# Alias REST endpoints (GET/DELETE /api/projects/<name>/aliases/<alias>)
# are intentionally OUT OF SCOPE for PR-C — they'd add ~200 LOC of
# registry-aware handlers without the surface-area pressure that
# motivates the other four. The pre-PR-C /__alias-usage and
# /__remove-alias endpoints remain in use; PR-F can fold them in.


# ── Legacy endpoints still work (back-compat for in-flight clients) ─


async def test_legacy_form_create_still_works(
    aiohttp_client, router_app,
) -> None:
    """The pre-PR-C form-encoded ``POST /__create`` is retained as
    DEPRECATED — keeps the dashboard's modals working until they
    migrate. PR-C doesn't remove it; a later major can."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__create",
        data={"name": "legacy-shape"},
        allow_redirects=False,
    )

    # The legacy handler 303-redirects to the index on success.
    assert resp.status == 303
