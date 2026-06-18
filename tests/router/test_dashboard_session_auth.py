"""Integration tests for Phase 1 PR D dashboard session auth.

PR D wires the operator session cookie (created by PR C's
``/agent-mcp/login`` flow) into the router's mutation surface:

  * The router's aiohttp middleware
    (``require_operator_session_middleware``) gates every
    ``/agent-mcp/...`` request EXCEPT the explicit unauth allow-list
    (login/logout/setup/assets/mcp + the ``/api/router/health``
    service descriptor).
  * Project-scoped paths
    (``/agent-mcp/api/<name>/...``, ``/agent-mcp/app/<name>/...``)
    also check ``project_membership`` for the resolved user.
  * Router-global mutations (``POST /agent-mcp/api/router/projects``,
    ``POST /agent-mcp/api/router/projects/<name>/agents``, etc.)
    require ANY logged-in operator (ADR 0014).
  * The legacy ``Authorization: Bearer <admin_token>`` path stays
    valid for ``/agent-mcp/mcp/<name>`` (MCP transport) and the
    per-project REST surface — agents must keep authenticating.

The tests intentionally bypass the dashboard JS and exercise the
HTTP contract directly.
"""

from __future__ import annotations

import pytest


# These tests exercise the auth gate directly, so we MUST start
# without the sentinel-operator cookie the router/conftest.py
# auto-attaches via the overridden ``aiohttp_client`` fixture —
# otherwise the 401 path can't be observed.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


# ── Helpers ─────────────────────────────────────────────────────────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username: str, password: str = "pw") -> str:
    """Create a user; return its user_id."""
    return _identity_module().create_user(
        username=username, password=password,
    )


async def _login(client, username: str, password: str = "pw") -> str:
    """POST /agent-mcp/login and return the session cookie value."""
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie, "expected Set-Cookie on successful login"
    # Cookie header form: "agent_mcp_session=<id>; Path=...; HttpOnly; ..."
    name_val = set_cookie.split(";", 1)[0]
    name, _, value = name_val.partition("=")
    assert name.strip() == "agent_mcp_session"
    return value.strip()


# ── 401 surface for dashboard mutation routes ──────────────────────


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


async def test_mutation_without_cookie_returns_401(
    aiohttp_client, router_app,
) -> None:
    """A POST to a router-admin mutation without the session cookie
    must 401 — PR D closes the "anyone with the URL can hit it"
    dashboard-side hole.
    """
    import json

    _seed_user("alice")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "proj-x"}),
        headers=_REST_HEADERS,
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


async def test_router_global_route_requires_session(
    aiohttp_client, router_app,
) -> None:
    """Router-global READ surfaces (``/api/router/projects``,
    ``/api/router/overview``) used by the operator-facing dashboard
    require a logged-in user too — Phase 1 doesn't yet split read
    vs write perms.

    Phase 3 adds finer system-perm gating; for now any logged-in
    operator can call these.
    """
    _seed_user("alice")
    client = await aiohttp_client(router_app)

    # Without cookie: 401.
    resp = await client.get(
        "/agent-mcp/api/router/overview",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()

    # With cookie: 200.
    cookie = await _login(client, "alice")
    resp = await client.get(
        "/agent-mcp/api/router/overview",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()


async def test_mutation_with_session_cookie_succeeds(
    aiohttp_client, router_app,
) -> None:
    """With a logged-in operator's cookie, POST to the create-project
    REST resource actually creates a project (or surfaces the
    registry's own validation error). The auth gate let us through
    to the handler.
    """
    import json

    _seed_user("alice")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "proj-y"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    # 201 on success; 4xx on validation; never 401 with a valid cookie.
    assert resp.status != 401, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


# ── Project membership ─────────────────────────────────────────────


async def test_project_scoped_route_requires_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """A user without ``project_membership`` for ``<project>`` cannot
    hit ``/agent-mcp/api/<project>/...`` — even though the same user
    can hit router-global routes.

    The first-operator retroactive-membership rule (PR B) applies
    to the FIRST user only; subsequent users need explicit grants.
    """
    register_project("alpha")
    # Seed two operators: alice (who joined first, retroactively a
    # member of everything) and bob (no memberships).
    _seed_user("alice")
    _seed_user("bob")
    client = await aiohttp_client(router_app)
    bob_cookie = await _login(client, "bob")

    resp = await client.get(
        "/agent-mcp/api/alpha/tokens",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        cookies={"agent_mcp_session": bob_cookie},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


@pytest.mark.no_seed_operator
async def test_first_operator_inherits_all_projects(
    aiohttp_client, router_app, register_project,
) -> None:
    """The FIRST operator created against a non-empty project
    registry inherits ``project_membership`` for each project (PR B
    contract). PR D's dep must honour those rows.

    We can't easily prove the backend reachability without spinning
    up a fake backend socket, so we settle for asserting that the
    middleware doesn't 401 us at the router edge — the request flows
    through to ``backend_api_handler`` (which may 502 when the
    backend isn't running, but 502 != 401 → dep passed).

    ``no_seed_operator`` skips the sentinel bootstrap so alice
    becomes the FIRST user — which triggers PR B's retroactive
    membership pass over the already-registered ``beta``.
    """
    register_project("beta")
    _seed_user("alice")
    client = await aiohttp_client(router_app)
    alice_cookie = await _login(client, "alice")

    resp = await client.get(
        "/agent-mcp/api/beta/tokens",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    # Anything that's not 401 means the dep accepted us. The backend
    # itself may not be reachable in test environment → 502/500/504
    # are all fine.
    assert resp.status != 401, await resp.text()


# ── Exempt paths still reachable without a cookie ──────────────────


async def test_login_page_is_reachable_without_cookie(
    aiohttp_client, router_app,
) -> None:
    """``GET /agent-mcp/login`` must NOT 401 — otherwise an
    unauthenticated operator would have no way to log in.
    """
    _seed_user("alice")
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/login", allow_redirects=False)
    assert resp.status == 200, await resp.text()


async def test_mcp_route_with_admin_bearer_still_works(
    aiohttp_client, router_app, register_project,
) -> None:
    """The legacy ``Authorization: Bearer <admin_token>`` path stays
    valid on ``/agent-mcp/mcp/<name>`` — agents that authenticate
    with the project admin token must keep working.

    The dep does NOT gate ``/mcp/`` paths; those routes have their
    own bearer-validation in ``backend_mcp_handler``. We only assert
    that the cookie middleware doesn't 401 a bearer-only request to
    the MCP route. (We don't have a real project admin token in the
    fixture without spinning up a backend, so we send a bogus bearer
    and check that the failure mode is bearer-validation 401 rather
    than the middleware's "no cookie" 401 — the wire shape differs:
    bearer-mode 401 carries no Set-Cookie or login-redirect hint.)
    """
    register_project("gamma")
    _seed_user("alice")  # Seed a user so the empty-users middleware
                         # doesn't bounce us to /setup.
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/gamma",
        headers={
            "Authorization": "Bearer bogus-but-syntactically-valid",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        allow_redirects=False,
    )
    # backend_mcp_handler 401s a bad bearer (we don't have a real
    # token) — that's fine. The KEY assertion is that we got the
    # bearer-validation 401 (no Set-Cookie/Location), not the
    # middleware's "redirect to login" 401.
    assert resp.status == 401, await resp.text()
    # Middleware-style 401 would surface a JSON envelope keyed on
    # ``login_required``; bearer 401 from backend_mcp_handler does
    # not. Smoke-test the discriminator:
    body = await resp.text()
    assert "login_required" not in body, (
        "MCP route 401 should be bearer-validation, not cookie-gate"
    )
