"""Tests for the ``?delete_workspace=true`` opt-in on
``DELETE /agent-mcp/api/router/projects/<name>`` (ADR 0014).

The remove modal's two-tier safe default (D4) lets the operator opt
into a hard `rm -rf` of the workspace files in the same request that
unregisters the project + stops its systemd unit. Default behaviour
unchanged: workspace is left intact unless the query string carries
``delete_workspace=true``.

The endpoint must:

1. Accept ``?delete_workspace=true`` (case-insensitive truthy values),
   remove the workspace directory recursively, and 200.
2. Default to leaving the workspace intact when the flag is absent or
   any non-truthy value.
3. Refuse with 409 + a structured body listing active agents if the
   project has any in-flight router-tracked sessions.
4. Refuse the workspace delete (200, ``workspace_deleted=false``,
   ``workspace_delete_skipped_reason`` set) if the workspace path
   resolves outside the configured ``AGENT_MCP_DEFAULT_WORKSPACE``
   parent. Defence in depth — the registry's stored workspace path
   drives the rm and a malicious projects.local.json edit shouldn't
   be able to wipe ``/``.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


async def test_unregister_default_leaves_workspace_intact(
    aiohttp_client, router_app, register_project,
) -> None:
    ws = register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/alpha",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["success"] is True
    assert body["workspace_deleted"] is False
    assert ws.exists(), "default unregister must leave the workspace dir"


async def test_unregister_with_delete_workspace_removes_dir(
    aiohttp_client, router_app, register_project, router_env,
) -> None:
    # Force the workspace under DEFAULT_WORKSPACE_PARENT so the
    # safety guard accepts the delete.
    ws = router_env.root / "workspaces" / "doomed"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "sentinel.txt").write_text("delete me")
    register_project("doomed", str(ws))
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/doomed?delete_workspace=true",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["unregistered"] == "doomed"
    assert body["workspace_deleted"] is True
    assert not ws.exists(), "workspace dir must be removed"


async def test_unregister_refuses_with_active_sessions(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project("busy")
    router_module.active_conns["busy"] = 2
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/busy",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "active_sessions"
    assert body["active_connections"] == 2


async def test_unregister_workspace_outside_safe_parent_skips_delete(
    aiohttp_client, router_app, router_env, router_module, tmp_path,
) -> None:
    """If the registered workspace lives outside DEFAULT_WORKSPACE_PARENT
    the endpoint MUST still unregister the project + stop the unit, but
    MUST refuse the rm -rf and surface ``workspace_deleted=false`` with
    ``workspace_delete_skipped_reason``."""
    outside = tmp_path / "outside-safe"
    outside.mkdir()
    (outside / "keep.txt").write_text("intact")
    router_module._REGISTRY.register("rogue", str(outside))
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/rogue?delete_workspace=true",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["workspace_deleted"] is False
    assert "outside" in body["workspace_delete_skipped_reason"].lower()
    assert outside.exists(), "out-of-tree workspace MUST not be removed"
