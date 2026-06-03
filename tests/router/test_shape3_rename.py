"""PR-B — Shape-3 URL rename.

Locked design (from /grill-me, PR plan):

  Old surface (Phase 6)           →   New surface (Shape 3, this PR)
  ───────────────────────────────────────────────────────────────────
  /agent-mcp/__api/<name>/<rest>  →   /agent-mcp/api/<name>/<rest>
  /agent-mcp/__dashboard/         →   /agent-mcp/app/
  /agent-mcp/__dashboard/<name>/  →   /agent-mcp/app/<name>/
  /agent-mcp/__dashboard/_next/   →   /agent-mcp/assets/

The MCP transport URL (/agent-mcp/<name>/mcp) is NOT moved in this PR —
that's PR-D (/agent-mcp/mcp/<name>). PR-B is scoped to the dashboard +
REST surface so the dashboard / REST rename can land cleanly without
the MCP client-config rewrite churn from PR-D's URL move.

Other PR-B changes pinned by these tests:
  - 308 redirects from every old path to the equivalent new path
    (30-day operator grace period; cheap courtesy per Dennis).
  - Service descriptor reflects the new URL surface.
  - ``_validate_name`` reserves ``api``, ``app``, ``assets``, ``mcp``
    so a project named after a top-level path segment cannot collide
    with the route table (audit §2.6).
  - ASSET_PREFIX env default migrates to /agent-mcp/assets so the
    sentinel substitution rewrites Next.js's asset URLs to the new
    top-level prefix (audit dashboard-bundle observation in §2.1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── Backend stand-in (lifted from test_proxy_passthrough) ──────────


class _FakeBackend:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def _handle(self, req: web.Request) -> web.Response:
        body = await req.read()
        self.records.append({
            "method": req.method, "path": req.path,
            "headers": dict(req.headers), "body": body,
            "query": dict(req.rel_url.query),
        })
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


# ── New REST surface: /agent-mcp/api/<name>/<rest> ──────────────────


async def test_new_api_path_routes_to_backend(
    aiohttp_client, router_app, fake_backend,
) -> None:
    """The renamed REST path (without __ prefix) reaches the backend.
    Strict Accept header still required (PR-A gate carried over)."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/proj/tokens",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    assert len(fake_backend.records) == 1
    assert fake_backend.records[0]["path"] == "/api/tokens"


async def test_new_api_path_keeps_accept_gate(
    aiohttp_client, router_app, register_project,
) -> None:
    """The Accept-header gate carries over verbatim to the renamed path
    — same 406 body, same behaviour, just at /api/<name>/ now."""
    register_project("proj")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/api/proj/tokens")

    assert resp.status == 406
    body = await resp.json()
    assert body["error"] == "version_required"


# ── Old REST path 308-redirects to new ───────────────────────────────


async def test_old_api_path_redirects_to_new(
    aiohttp_client, router_app, register_project,
) -> None:
    """The pre-PR-B path /agent-mcp/__api/<name>/<rest> 308-redirects
    to /agent-mcp/api/<name>/<rest> so external services that hard-coded
    the URL keep working for ~30 days. 308 (not 302) preserves the
    method + body across the redirect."""
    register_project("proj")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__api/proj/tokens",
        allow_redirects=False,
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 308
    assert resp.headers["Location"] == "/agent-mcp/api/proj/tokens"


async def test_old_api_path_redirect_preserves_query_string(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("proj")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__api/proj/messages?filter=unread",
        allow_redirects=False,
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 308
    assert (
        resp.headers["Location"]
        == "/agent-mcp/api/proj/messages?filter=unread"
    )


# ── New dashboard surface: /agent-mcp/app/<name>/ ───────────────────


async def test_new_app_path_serves_dashboard(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """The renamed dashboard path (/app/) serves the same on-disk
    index.html the /__dashboard/ path used to. One on-disk tree,
    two URL paths during the 30-day grace period."""
    write_dashboard_file("index.html", "<html>dashboard</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/proj/")

    assert resp.status == 200
    assert "dashboard" in await resp.text()


async def test_new_app_bare_path_serves_overview(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """The bare /agent-mcp/app/ path serves the React overview
    (cross-project cards) — same handler as old /__dashboard/ did."""
    write_dashboard_file("index.html", "<html>overview</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/")

    assert resp.status == 200


async def test_old_dashboard_path_redirects_to_app(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """The pre-PR-B /agent-mcp/__dashboard/<name>/ path 308-redirects to
    /agent-mcp/app/<name>/. Bookmarks and external links survive the
    rename for 30 days."""
    write_dashboard_file("index.html", "<html>dashboard</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/proj/", allow_redirects=False,
    )

    assert resp.status == 308
    assert resp.headers["Location"] == "/agent-mcp/app/proj/"


# ── New asset surface: /agent-mcp/assets/<path> ─────────────────────


async def test_new_assets_path_serves_next_static_bundle(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """Assets move from /agent-mcp/__dashboard/_next/<rest> to
    /agent-mcp/assets/<rest> — a top-level prefix, decoupled from the
    /app/ pages path. Tested via a JS file under _next/static/ (the
    on-disk layout Next.js emits) since that's what asset_prefix
    substitution actually rewrites references to."""
    write_dashboard_file("_next/static/chunks/main.js", "console.log('asset')")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/assets/static/chunks/main.js")

    assert resp.status == 200
    assert "console.log" in await resp.text()
    assert resp.content_type == "application/javascript"


async def test_old_assets_path_redirects_to_new(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """Old asset path 308-redirects so cached HTML pages from before
    the rename still resolve assets (within the 30-day window)."""
    write_dashboard_file("_next/static/chunks/main.js", "x")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/_next/static/chunks/main.js",
        allow_redirects=False,
    )

    assert resp.status == 308
    assert (
        resp.headers["Location"]
        == "/agent-mcp/assets/static/chunks/main.js"
    )


# ── Service descriptor reflects new URLs ─────────────────────────────


async def test_descriptor_advertises_new_endpoint_urls(
    aiohttp_client, router_app,
) -> None:
    """A client following the descriptor lands on the renamed surface
    — proves the descriptor and the route table agree."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/", headers={"Accept": "application/json"},
    )

    body = await resp.json()
    eps = body["endpoints"]
    assert eps["api"] == "/agent-mcp/api"
    assert eps["app"] == "/agent-mcp/app"
    assert eps["assets"] == "/agent-mcp/assets"
    # PR-D will move this to /agent-mcp/mcp/<name>; PR-B keeps the
    # current per-project shape but the descriptor still names it.
    assert eps["mcp"] == "/agent-mcp"


# ── Reserved-name validator (audit §2.6) ────────────────────────────


async def test_reserved_top_level_names_rejected(router_module) -> None:
    """The slug regex doesn't reserve any names today — a project
    literally named ``api`` would become unreachable behind the renamed
    /api/* route. ``_validate_name`` MUST reject the four top-level
    path segments now used by the URL surface."""
    for reserved in ("api", "app", "assets", "mcp"):
        err = router_module._validate_name(reserved, existing={})
        assert err is not None, f"name {reserved!r} should be rejected"
        assert "reserved" in err.lower(), (
            f"error for {reserved!r} should mention 'reserved', got: {err!r}"
        )


async def test_non_reserved_names_still_accepted(router_module) -> None:
    """Defensive — make sure the reservation didn't accidentally
    reject every name. ``api-server`` is not reserved; ``apis`` either."""
    for ok in ("api-server", "apis", "appendix", "asset-tracker", "mcps"):
        err = router_module._validate_name(ok, existing={})
        assert err is None, f"name {ok!r} should be accepted, got: {err!r}"


# ── Old /__projects, /__overview etc. unchanged ──────────────────────


async def test_old_direct_router_routes_unchanged(
    aiohttp_client, router_app, register_project,
) -> None:
    """The direct router endpoints (/__projects, /__overview, /__create,
    /__rename, /__unregister, /__alias-usage, /__remove-alias,
    /__client-config, /__client-installer) are NOT renamed in PR-B —
    PR-C folds the project-lifecycle ones into POST /api/projects.
    Verify a sample still resolves so PR-B doesn't accidentally break
    them."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__projects")

    assert resp.status == 200
    body = await resp.json()
    assert body == {"projects": ["alpha"]}
