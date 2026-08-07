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


# Old REST path 308 redirects were dropped in v5.0.0 after the 30-day
# grace window. test_legacy_url_removal.py now asserts /agent-mcp/__api/
# 404s; the historical 308 behaviour pinned by this section is gone.


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


# The /agent-mcp/__dashboard/<name>/ 308 redirect was dropped in
# v5.0.0. test_legacy_url_removal.py asserts the 404 behaviour.


# ── New asset surface: /agent-mcp/assets/<path> ─────────────────────


async def test_new_assets_path_serves_next_static_bundle(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """Assets move from /agent-mcp/__dashboard/_next/<rest> to
    /agent-mcp/assets/_next/<rest> — the parent prefix is renamed
    to a top-level segment, but Next.js's own ``_next/`` artifact
    stays in the URL because that's what the dashboard's webpack
    runtime emits in chunk-resolution code (the sentinel substitution
    replaces only the prefix, not the ``_next`` suffix Next.js
    appends in its own runtime). New default ASSET_PREFIX is
    /agent-mcp/assets, so the substituted HTML/JS emits this URL
    shape."""
    write_dashboard_file("_next/static/chunks/main.js", "console.log('asset')")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/assets/_next/static/chunks/main.js")

    assert resp.status == 200
    assert "console.log" in await resp.text()
    assert resp.content_type == "application/javascript"


# The /agent-mcp/__dashboard/_next/<rest> 308 redirect was dropped in
# v5.0.0; cached HTML pages from before the rename no longer resolve
# assets via the legacy URL. test_legacy_url_removal.py asserts the
# 404 behaviour.


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
    # PR-D moved the MCP transport to /agent-mcp/mcp/<name>; the
    # descriptor advertises the parent prefix.
    assert eps["mcp"] == "/agent-mcp/mcp"


# ── Reserved-name validator (audit §2.6) ────────────────────────────


async def test_reserved_top_level_names_rejected(router_module) -> None:
    """The slug regex doesn't reserve any names today — a project
    literally named ``api`` would become unreachable behind the renamed
    /api/* route. ``_validate_name`` MUST reject the five top-level
    path segments now used by the URL surface. ``router`` is the
    admin-namespace segment added by ADR 0014."""
    for reserved in ("api", "app", "assets", "mcp", "router"):
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


# ── Router admin surface (ADR 0014) ──────────────────────────────────


async def test_router_admin_projects_listed(
    aiohttp_client, router_app, register_project,
) -> None:
    """Sanity guard: the project list at the new admin URL resolves
    and returns the JSON envelope the dashboard consumes. The legacy
    ``__projects`` shape was retired in ADR 0014; the new path is
    ``/api/router/projects``."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
    )

    assert resp.status == 200
    body = await resp.json()
    assert body == {"projects": ["alpha"]}
