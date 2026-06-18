"""Tests for the router rename endpoint, alias-in-proxy plumbing,
and the alias reaper background task (Phase 1b).

ADR 0014 moved the rename surface from the legacy
``POST /agent-mcp/__rename`` (form-encoded) to
``PATCH /agent-mcp/api/router/projects/<name>`` (JSON body), but the
underlying registry semantics + active-session refusal + grace-alias
behaviour are unchanged.

The registry data-model tests live next door in
``test_project_registry.py`` — this file is for the router-layer
plumbing that consumes the new registry primitives.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# Every async test in this file uses the asyncio loop fixture from
# pytest-asyncio. Apply once at module scope so individual tests stay
# readable.
pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── PATCH /api/router/projects/<name> ──────────────────────────────


async def test_rename_endpoint_happy_path(
    aiohttp_client, router_app, router_module, register_project,
    systemctl_stub,
) -> None:
    ws = register_project("old-name")
    assert ws.exists()

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/old-name",
        data=json.dumps({"name": "new-name", "grace_days": 7}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["renamed"] == {"from": "old-name", "to": "new-name"}
    assert body["alias"]["name"] == "old-name"
    assert "expires_at" in body["alias"]

    # Registry was updated.
    assert router_module._REGISTRY.get("old-name") is None
    new_row = router_module._REGISTRY.get("new-name")
    assert new_row is not None
    assert router_module._REGISTRY.resolve_alias("old-name") == "new-name"

    # The on-disk workspace was renamed (the original ws path ends
    # in /old-name, so the endpoint renames it to /new-name).
    assert not ws.exists()
    assert ws.with_name("new-name").is_dir()

    # And the systemd unit for the old name was asked to stop.
    stops = [
        c for c in systemctl_stub.calls
        if len(c) >= 2 and c[0] == "stop"
        and c[1] == "agent-mcp@old-name.service"
    ]
    assert stops, f"expected systemctl stop call; saw {systemctl_stub.calls!r}"


async def test_rename_endpoint_rejects_existing_new_name(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/alpha",
        data=json.dumps({"name": "beta"}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 409


async def test_rename_endpoint_rejects_inflight_session(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("busy-project")
    router_module.active_conns["busy-project"] = 2
    try:
        client = await aiohttp_client(router_app)
        resp = await client.patch(
            "/agent-mcp/api/router/projects/busy-project",
            data=json.dumps({"name": "calm-project"}),
            headers=_STRICT_ACCEPT,
        )
        assert resp.status == 409
        body = await resp.json()
        # Body must surface the in-flight count so the operator knows
        # what's blocking them.
        assert body.get("active_connections", 0) >= 1
        # And the rename did NOT happen.
        assert router_module._REGISTRY.get("busy-project") is not None
        assert router_module._REGISTRY.get("calm-project") is None
    finally:
        router_module.active_conns.pop("busy-project", None)


async def test_rename_endpoint_rejects_bad_slug(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/alpha",
        data=json.dumps({"name": "Bad_Slug"}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 400


# ── Alias resolution in proxy path ──────────────────────────────────


async def test_alias_resolves_in_mcp_proxy_with_header_injection(
    aiohttp_client, router_app, router_module, router_env,
    register_project, monkeypatch,
) -> None:
    """Request to /agent-mcp/<alias>/mcp reaches the backend with the
    X-Agent-MCP-Alias header set, addressed to the real project."""
    from aiohttp import web as aweb

    register_project("real-project")
    router_module._REGISTRY.add_alias("real-project", "old-alias")

    sock_dir = router_env.sock_dir / "real-project"
    sock_dir.mkdir(parents=True, exist_ok=True)
    backend_sock = sock_dir / "backend.sock"

    captured: list = []

    async def _echo(req):
        body = await req.read()
        captured.append(
            {
                "method": req.method,
                "path": req.path,
                "headers": dict(req.headers),
                "body": body,
            }
        )
        return aweb.Response(text="ok", status=200)

    backend_app = aweb.Application()
    backend_app.router.add_route("*", "/{tail:.*}", _echo)
    runner = aweb.AppRunner(backend_app)
    await runner.setup()
    site = aweb.UnixSite(runner, str(backend_sock))
    await site.start()

    try:
        async def fake_ensure(name, role):
            return backend_sock

        monkeypatch.setattr(router_module, "_ensure", fake_ensure)

        async def fake_tokens(name):
            return {"test-token": "Admin"}

        monkeypatch.setattr(router_module, "_agent_token_map", fake_tokens)

        client = await aiohttp_client(router_app)
        # v5.0.0: MCP transport URL is /agent-mcp/mcp/<name>.
        resp = await client.post(
            "/agent-mcp/mcp/old-alias",
            data=b"{}",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 200, await resp.text()
        assert len(captured) == 1
        recv = captured[0]
        assert recv["path"] == "/mcp"
        alias_hdr = recv["headers"].get("X-Agent-MCP-Alias")
        assert alias_hdr is not None, (
            f"missing X-Agent-MCP-Alias; saw {sorted(recv['headers'])!r}"
        )
        parts = alias_hdr.split(",", 1)
        assert parts[0] == "old-alias"
        assert "T" in parts[1]  # ISO-8601 timestamp
    finally:
        await runner.cleanup()


async def test_unknown_name_with_no_matching_alias_rejects(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("real-project")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/totally-unknown/mcp",
        headers={"Authorization": "Bearer anything"},
        data=b"{}",
    )
    # Either 401 (token rejected first) or 404 (unknown project) is
    # acceptable — what matters is that the request does NOT succeed.
    assert resp.status in (401, 404)


# ── Alias reaper ────────────────────────────────────────────────────


async def test_alias_reaper_removes_expired_entries(
    router_module, tmp_path: Path, caplog, monkeypatch,
) -> None:
    """Single tick of the reaper drops past-due aliases and logs."""
    from agent_mcp.router import project_registry

    # Repoint the registry at a tmp file so we don't share state with
    # other tests in the same worker.
    monkeypatch.setattr(
        project_registry, "REGISTRY_PATH", tmp_path / "projects.local.json",
    )
    reg = project_registry.ProjectRegistry()
    reg.register("alpha", "/tmp/alpha")
    past = _iso(datetime.now(timezone.utc) - timedelta(seconds=1))
    future = _iso(datetime.now(timezone.utc) + timedelta(days=10))
    reg.add_alias("alpha", "expired-one", expires_at=past)
    reg.add_alias("alpha", "alive-one", expires_at=future)

    caplog.set_level(logging.INFO, logger="agent_mcp.router.app")
    await router_module._alias_reaper_tick(reg)

    row = reg.get("alpha")
    alias_names = {a["name"] for a in row["aliases"]}
    assert alias_names == {"alive-one"}
    assert any(
        "expired-one" in r.message and "alpha" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]
