"""PR-A — Service descriptor + Accept-header gate on REST surface.

Locked design (from /grill-me, recorded in the URL-redesign plan):

  GET /agent-mcp/                  → JSON service descriptor (Accept-negotiated)
                                     302 → /__dashboard/ when Accept: text/html
  /agent-mcp/__api/<name>/<rest>   → strict Accept gate
                                     application/vnd.agent-mcp.v1+json required
                                     406 with structured error body otherwise

This is PR-A (smallest blast radius) — the URL surface is NOT renamed
in this PR; we add the descriptor at the existing /agent-mcp/ URL and
gate the existing /agent-mcp/__api/* proxy. The rename happens in PR-B.

The §3.7 audit finding (tokens endpoint auth gap) is folded in: the
Accept-header gate covers every REST endpoint uniformly, so an
attacker can no longer skip auth just by omitting the Authorization
header — the request now fails at the Accept gate first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = pytest.mark.asyncio


# ── Backend stand-in (lifted from test_proxy_passthrough — keeping a
#    local copy avoids the conftest.py reshuffle that would otherwise
#    touch every router test file) ─────────────────────────────────────


class _FakeBackend:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.response_factory: Callable[[web.Request], Awaitable[web.Response]] | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def _handle(self, req: web.Request) -> web.Response:
        body = await req.read()
        self.records.append(
            {
                "method": req.method,
                "path": req.path,
                "headers": dict(req.headers),
                "body": body,
                "query": dict(req.rel_url.query),
            },
        )
        if self.response_factory is not None:
            return await self.response_factory(req)
        return web.Response(body=b'{"ok":true}', content_type="application/json")


async def _start_backend_on_uds(backend: _FakeBackend, sock_path: Path) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def fake_backend(router_module, router_env, systemctl_stub):
    """UDS backend at the path the proxy expects for project 'proj'.
    Project is pre-registered, systemctl unit is marked active."""
    name = "proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _FakeBackend()
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()


# ── Service descriptor ───────────────────────────────────────────────


async def test_index_returns_service_descriptor_for_json_client(
    aiohttp_client, router_app, register_project,
) -> None:
    """A non-browser client hitting GET /agent-mcp/ (no Accept header,
    or Accept: application/json) gets the JSON service descriptor.

    The descriptor is the discovery document for the public URL surface
    — operators paste a base URL into a tool, that tool fetches the
    descriptor, and follows the embedded endpoint links."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/", allow_redirects=False)

    assert resp.status == 200
    assert resp.content_type == "application/json"
    body = await resp.json()
    assert body["service"] == "agent-mcp"
    assert "version" in body and body["version"]
    assert body["mode"] == "multi-tenant"
    # The endpoint URLs are the Shape-3 surface (PR-B). They MUST
    # point at actual mounted routes — see ``make_app`` wire-up.
    assert "endpoints" in body
    eps = body["endpoints"]
    assert eps["api"] == "/agent-mcp/api"
    assert eps["app"] == "/agent-mcp/app"
    assert eps["assets"] == "/agent-mcp/assets"
    # PR-D moved the MCP transport to /agent-mcp/mcp/<name>; the
    # descriptor advertises the parent prefix.
    assert eps["mcp"] == "/agent-mcp/mcp"
    # Discovery links the dashboard's two READ entry points so a plain
    # HTTP client can iterate projects without scraping HTML.
    assert body["projects_url"] == "/agent-mcp/__projects"
    assert body["overview_url"] == "/agent-mcp/__overview"
    assert body["single_tenant_project"] is None


async def test_index_redirects_browser_to_dashboard(
    aiohttp_client, router_app,
) -> None:
    """A browser sends Accept: text/html — that case still 302s to the
    React dashboard (existing behaviour from Phase 3.5a / ADR-0009).

    The Accept-negotiated split keeps backwards compat for humans
    pasting the URL into a browser bar."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/",
        headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        allow_redirects=False,
    )

    assert resp.status == 302
    assert resp.headers["Location"] == "/agent-mcp/app/"


async def test_index_descriptor_reports_single_tenant_project(
    aiohttp_client, router_module, register_project,
) -> None:
    """In single-tenant mode the descriptor identifies the one project
    so a client can short-circuit project-picker logic entirely."""
    register_project("only")
    app = router_module.make_app(single_tenant_name="only")
    client = await aiohttp_client(app)

    resp = await client.get(
        "/agent-mcp/",
        headers={"Accept": "application/json"},
        allow_redirects=False,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["mode"] == "single-tenant"
    assert body["single_tenant_project"] == "only"


async def test_index_browser_redirects_to_single_tenant_dashboard(
    aiohttp_client, router_module, register_project,
) -> None:
    """The browser-redirect in single-tenant mode still goes straight
    to the configured project's dashboard (existing decision #2)."""
    register_project("only")
    app = router_module.make_app(single_tenant_name="only")
    client = await aiohttp_client(app)

    resp = await client.get(
        "/agent-mcp/",
        headers={"Accept": "text/html"},
        allow_redirects=False,
    )

    assert resp.status == 302
    assert resp.headers["Location"] == "/agent-mcp/app/only/"


# ── Accept-header gate on /__api/* ───────────────────────────────────


async def test_api_proxy_rejects_request_without_strict_accept_header(
    aiohttp_client, router_app, register_project,
) -> None:
    """The REST surface MUST require the versioned Accept header. A
    client that doesn't send it gets 406 with a structured error body
    explaining how to fix it. Folds the §3.7 tokens auth gap shut: the
    gate runs before any per-endpoint auth logic, so the previously-
    unauthenticated tokens endpoint can no longer be reached by an
    unversioned request."""
    register_project("proj")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__api/proj/tokens")

    assert resp.status == 406
    body = await resp.json()
    assert body["error"] == "version_required"
    assert body["supported_versions"] == ["v1"]
    assert body["current_default"] == "v1"
    # The message must tell the caller the exact header to add — the
    # error body is the diagnostic; clients don't need to read docs to
    # fix the request.
    assert "application/vnd.agent-mcp.v1+json" in body["message"]
    assert body["docs"].startswith("https://")


async def test_api_proxy_rejects_request_with_wrong_accept_header(
    aiohttp_client, router_app, register_project,
) -> None:
    """A generic Accept: application/json or Accept: */* is not enough
    — we want explicit, version-pinned consent so that an upgrade to
    v2 can land alongside a v1-locked default without silently breaking
    callers that asked for ‘any JSON’."""
    register_project("proj")
    client = await aiohttp_client(router_app)

    for accept in ("application/json", "*/*", "text/html"):
        resp = await client.get(
            "/agent-mcp/__api/proj/tokens",
            headers={"Accept": accept},
        )
        assert resp.status == 406, f"Accept: {accept!r} should be rejected"


async def test_api_proxy_accepts_request_with_strict_accept_header(
    aiohttp_client, router_app, fake_backend,
) -> None:
    """The well-formed Accept header passes the gate and the request
    flows through to the backend. ``fake_backend`` is the same UDS
    backend the proxy tests use; project 'proj' is pre-registered.

    We assert the proxy actually reached the backend by checking the
    backend recorded one request — proves the Accept gate is letting
    the request through, not silently 200-ing on the router."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__api/proj/tokens",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
    )

    assert resp.status == 200
    assert len(fake_backend.records) == 1
    assert fake_backend.records[0]["path"] == "/api/tokens"


async def test_api_proxy_accepts_strict_accept_with_parameters(
    aiohttp_client, router_app, fake_backend,
) -> None:
    """Accept headers can carry quality parameters (q=0.9) per RFC 7231.
    The gate must accept ``application/vnd.agent-mcp.v1+json;q=0.9``
    just as it accepts the bare media type."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__api/proj/tokens",
        headers={"Accept": "application/vnd.agent-mcp.v1+json;q=0.9"},
    )

    assert resp.status == 200


async def test_api_proxy_accepts_strict_accept_inside_multi_value(
    aiohttp_client, router_app, fake_backend,
) -> None:
    """A client that sends multiple acceptable types in one header (the
    common case for tools that also accept text/plain for errors) must
    pass as long as the versioned type is somewhere in the list."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__api/proj/tokens",
        headers={
            "Accept": "text/plain;q=0.1, application/vnd.agent-mcp.v1+json"
        },
    )

    assert resp.status == 200


# ── MCP transport is NOT gated by the Accept header ─────────────────


async def test_mcp_endpoint_does_not_require_accept_header(
    aiohttp_client, router_app, fake_backend, router_module,
) -> None:
    """The MCP transport has its own version negotiation
    (initialize.protocolVersion). Adding our Accept gate to it would
    break every MCP client. Verify the MCP URL still works with no
    Accept header at all — the Accept gate is /__api/-scoped only."""
    router_module._agent_token_cache["proj"] = (
        9.9e18, {"tok-1234": "Admin"},
    )
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/proj/mcp",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        headers={
            "Authorization": "Bearer tok-1234",
            "Content-Type": "application/json",
        },
    )

    assert resp.status == 200


# ── Dashboard routes are NOT gated either ────────────────────────────


async def test_dashboard_route_does_not_require_accept_header(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """The dashboard HTML / asset routes are for browsers, not API
    callers — they MUST NOT require the versioned Accept header."""
    write_dashboard_file("index.html", "<html>dashboard</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/")

    assert resp.status == 200
    assert "dashboard" in await resp.text()
