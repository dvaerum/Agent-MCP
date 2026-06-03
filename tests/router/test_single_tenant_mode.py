"""Unit tests for single-tenant router mode (Phase 3 of the
router-upstream plan, prancy-napping-pie).

When the router is started with a ``single_tenant_name`` (set via
``make_app(single_tenant_name=..., single_tenant_workspace=...)`` or
the CLI's ``--single-tenant <name> --single-workspace <ws>`` flags),
the multi-tenant write surface is disabled and any URL pointing at a
project other than the configured one is W1-redirected to the same
section path under the configured project.

Five cases mirror the VM scaffolds added in the same PR:

1. ``__create`` POSTs → 410 with the documented error body shape.
2. ``__unregister`` POSTs → 410 with the documented error body shape.
3. ``__rename`` POSTs → 410 with the documented error body shape.
4. ``/__dashboard/<wrong-name>/<section>`` → 302 to
   ``/__dashboard/<configured-name>/<section>`` (W1 redirect; preserves
   the section path so the URL bar lands on the right page in the
   single-tenant project).
5. ``/<wrong-name>/mcp`` and ``/__api/<wrong-name>/{rest}`` → 302 to the
   same path under the configured single-tenant project.

Multi-tenant default behaviour is exercised by the rest of the suite;
this file's fixtures construct a *second* app via
``make_app(single_tenant_name=...)`` so we can assert the toggle's
effect without re-importing the module.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def single_tenant_app(router_module, register_project):
    """An aiohttp app constructed with ``single_tenant_name=`only`-project``.

    The project is pre-registered in the test registry so the proxy
    routes have a real entry to point at; the redirect logic itself
    doesn't reach the registry (it short-circuits on URL match), but
    the dashboard handler's SPA fallback wants a sane registry too.
    """
    register_project("only-project")
    return router_module.make_app(
        single_tenant_name="only-project",
        single_tenant_workspace=None,
    )


# ── 1. __create disabled ───────────────────────────────────────────


async def test_single_tenant_disables_create(
    aiohttp_client, single_tenant_app,
) -> None:
    client = await aiohttp_client(single_tenant_app)
    resp = await client.post("/agent-mcp/__create", data={"name": "newproj"})
    assert resp.status == 410
    body = await resp.json()
    assert body == {
        "error": "endpoint_disabled_in_single_tenant_mode",
        "single_tenant_name": "only-project",
    }


# ── 2. __unregister disabled ───────────────────────────────────────


async def test_single_tenant_disables_unregister(
    aiohttp_client, single_tenant_app,
) -> None:
    client = await aiohttp_client(single_tenant_app)
    resp = await client.post(
        "/agent-mcp/__unregister", data={"name": "only-project"},
    )
    assert resp.status == 410
    body = await resp.json()
    assert body == {
        "error": "endpoint_disabled_in_single_tenant_mode",
        "single_tenant_name": "only-project",
    }


# ── 3. __rename disabled ───────────────────────────────────────────


async def test_single_tenant_disables_rename(
    aiohttp_client, single_tenant_app,
) -> None:
    client = await aiohttp_client(single_tenant_app)
    resp = await client.post(
        "/agent-mcp/__rename",
        data={"old_name": "only-project", "new_name": "renamed"},
    )
    assert resp.status == 410
    body = await resp.json()
    assert body == {
        "error": "endpoint_disabled_in_single_tenant_mode",
        "single_tenant_name": "only-project",
    }


# ── 4. W1 redirect — dashboard ─────────────────────────────────────


async def test_single_tenant_dashboard_wrong_name_redirects(
    aiohttp_client, single_tenant_app,
) -> None:
    client = await aiohttp_client(single_tenant_app)
    # Hit the dashboard URL for a project that is NOT the configured
    # single-tenant project. Disable client-side redirect-follow so we
    # observe the 302 itself rather than its target.
    resp = await client.get(
        "/agent-mcp/app/some-other-project/tasks/",
        allow_redirects=False,
    )
    assert resp.status == 302
    assert (
        resp.headers["Location"]
        == "/agent-mcp/app/only-project/tasks/"
    )


async def test_single_tenant_dashboard_bare_wrong_name_redirects(
    aiohttp_client, single_tenant_app,
) -> None:
    """The bare ``/app/<name>`` (no trailing slash) form is a
    real route too — make sure W1 redirect catches it before the
    bare→trailing-slash 301 fires."""
    client = await aiohttp_client(single_tenant_app)
    resp = await client.get(
        "/agent-mcp/app/some-other-project/",
        allow_redirects=False,
    )
    assert resp.status == 302
    assert (
        resp.headers["Location"]
        == "/agent-mcp/app/only-project/"
    )


# ── 5. W1 redirect — MCP + API proxy ───────────────────────────────


async def test_single_tenant_mcp_wrong_name_redirects(
    aiohttp_client, single_tenant_app,
) -> None:
    """Wrong-project MCP URL → W1 redirect to the configured project's
    MCP URL.

    PR-D moved the MCP path from /agent-mcp/<name>/mcp to
    /agent-mcp/mcp/<name>; this test uses the new shape. The W1
    single-tenant substitution swaps the project name segment, which
    in the new shape is the last segment."""
    client = await aiohttp_client(single_tenant_app)
    resp = await client.post(
        "/agent-mcp/mcp/some-other-project",
        headers={"Authorization": "Bearer dummy-token"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert resp.headers["Location"] == "/agent-mcp/mcp/only-project"


async def test_single_tenant_api_wrong_name_redirects(
    aiohttp_client, single_tenant_app,
) -> None:
    """PR-A: the REST surface now requires the strict Accept header.
    The single-tenant W1 redirect runs after the Accept gate, so the
    test must opt in to the gate to reach the redirect path."""
    client = await aiohttp_client(single_tenant_app)
    resp = await client.get(
        "/agent-mcp/api/some-other-project/agents",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert (
        resp.headers["Location"]
        == "/agent-mcp/api/only-project/agents"
    )


# ── Sanity: configured name is unaffected ──────────────────────────


async def test_single_tenant_configured_name_no_redirect(
    aiohttp_client, single_tenant_app, write_dashboard_file,
) -> None:
    """A request to the *correct* single-tenant project must NOT
    redirect — it serves normally (here we use the dashboard handler
    since it has a useful 200 path that doesn't need a real backend)."""
    write_dashboard_file("index.html", "<html>only</html>")
    client = await aiohttp_client(single_tenant_app)
    resp = await client.get(
        "/agent-mcp/app/only-project/",
        allow_redirects=False,
    )
    assert resp.status == 200
    assert "only" in await resp.text()


# ── Sanity: multi-tenant default keeps write endpoints open ────────


async def test_multi_tenant_default_still_allows_create(
    aiohttp_client, router_app,
) -> None:
    """Regression guard: the toggle defaults off, so a router built
    via the regular ``router_app`` fixture (which calls ``make_app()``
    with no args) still accepts ``__create`` POSTs as before."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/__create",
        data={"name": "fresh-project"},
        allow_redirects=False,
    )
    # __create returns 303 See Other on success (or 400 on validation
    # error). 410 would mean we accidentally engaged single-tenant
    # mode in the default router; assert we did not.
    assert resp.status != 410
    assert resp.status in (303, 400)
