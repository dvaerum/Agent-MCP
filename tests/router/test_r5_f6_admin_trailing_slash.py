"""R5-F6 [LOW, routing]: a trailing slash on the router's admin-only
``/agent-mcp/api/router/...`` surface (projects/users/groups/
memberships/SSO CRUD) bypassed the route-specific handler entirely and
fell through to the generic per-project proxy
(``backend_api_handler``), which treats the first path segment as a
PROJECT NAME. That fallthrough currently dead-ended in a 404 ONLY
because ``router`` is a member of ``_RESERVED_NAMES`` — an unrelated
naming-collision guard, not a structural denial of the fallthrough
site itself. Confirmed live: ``GET /api/router/projects`` -> 200
(correct admin route); ``GET /api/router/projects/`` (trailing slash)
-> 404 unknown project (fell through to ``backend_api_handler`` — a
distinct error shape from the admin 404, confirming the fallthrough).

The fix registers an explicit trailing-slash alias for every admin
route (``_add_admin_trailing_slash_aliases`` in ``agent_mcp/router/
app.py``), mirroring the existing ``_add_root_aliases`` pattern, so
the SAME route-specific (capability-gated) handler serves the
trailing-slash form too — closing the fallthrough at its source
instead of relying on the reserved-name side effect.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


async def test_trailing_slash_on_admin_list_resolves_to_admin_handler(
    aiohttp_client, router_app,
) -> None:
    """``GET /agent-mcp/api/router/projects/`` (trailing slash) must hit
    the SAME admin ``list_projects_handler`` as the no-slash form, not
    fall through to the per-project catch-all.

    Pre-fix (RED): the trailing-slash request 404s with the per-project
    proxy's "unknown project" shape (``backend_api_handler``'s error
    envelope for a project named "router" that can never exist) instead
    of the admin route's 200 project-list envelope.
    """
    client = await aiohttp_client(router_app)

    no_slash = await client.get(
        "/agent-mcp/api/router/projects", headers=_STRICT_ACCEPT,
    )
    assert no_slash.status == 200, await no_slash.text()
    no_slash_body = await no_slash.json()

    trailing_slash = await client.get(
        "/agent-mcp/api/router/projects/", headers=_STRICT_ACCEPT,
    )
    assert trailing_slash.status == 200, (
        f"expected the admin list-projects handler (200), got "
        f"{trailing_slash.status}: {await trailing_slash.text()} — the "
        "trailing slash fell through to the per-project catch-all "
        "instead of resolving to the admin route"
    )
    trailing_slash_body = await trailing_slash.json()
    assert trailing_slash_body == no_slash_body


async def test_trailing_slash_on_admin_health_is_the_public_admin_route(
    aiohttp_client, router_app,
) -> None:
    """``GET /agent-mcp/api/router/health/`` must hit the public admin
    health handler (allow-listed, no session needed), not the
    per-project catch-all (which would 401/404 for an unauthenticated
    caller hitting a nonexistent "router" project).
    """
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/health/",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["service"] == "agent-mcp-router"


async def test_trailing_slash_on_admin_dynamic_segment_route(
    aiohttp_client, router_app,
) -> None:
    """A trailing slash after a dynamic ``{name}`` segment (e.g. the
    project rename/delete resource) must ALSO resolve to the admin
    handler, not the catch-all — proves the alias generalises beyond
    the static-path routes."""
    client = await aiohttp_client(router_app)

    create = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "trailing-slash-target"}),
        headers=_STRICT_ACCEPT,
    )
    assert create.status == 201, await create.text()

    resp = await client.delete(
        "/agent-mcp/api/router/projects/trailing-slash-target/",
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, (
        f"expected the admin delete-project handler (200), got "
        f"{resp.status}: {await resp.text()}"
    )


async def test_admin_routes_without_trailing_slash_still_work(
    aiohttp_client, router_app,
) -> None:
    """Happy-path regression guard: the canonical no-trailing-slash
    admin routes must be entirely unaffected by the alias addition."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects", headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert "projects" in body


@pytest.mark.no_auth_seed_session
async def test_trailing_slash_alias_preserves_capability_gate(
    aiohttp_client, router_app,
) -> None:
    """The trailing-slash alias must carry the SAME capability gate as
    the canonical route — an unauthenticated caller must be rejected
    (401), not silently admitted, and must NOT get the per-project
    catch-all's distinct 404 "unknown project" shape either."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects/", headers=_STRICT_ACCEPT,
    )
    assert resp.status in (401, 403), (
        f"expected an auth rejection for an unauthenticated caller, got "
        f"{resp.status}: {await resp.text()}"
    )
