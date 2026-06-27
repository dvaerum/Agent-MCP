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


async def test_mcp_route_with_operator_cookie_reaches_handler(
    aiohttp_client, router_app, register_project,
) -> None:
    """The dashboard's MCP notifications SSE subscription drops the
    bearer header and authenticates with the ``agent_mcp_session``
    cookie instead. The cookie path lives inside
    ``backend_mcp_handler`` (NOT ``require_operator_session_middleware``,
    which still allow-lists ``/agent-mcp/mcp/`` so the agent-side
    bearer path keeps working).

    retire-system-token Wave 2 (2026-06-23): the cookie path no
    longer translates to an admin bearer; the router signs a
    ``X-Agent-MCP-Forwarded-Operator`` header from the per-project
    HMAC key. F015 v4 (2026-06-23): the systemd unit's ExecStartPre
    owns the on-disk key; we pre-seed it directly here, mirroring
    what the unit does in production, so
    ``_forwarding_header_from_cookie`` can sign without standing up
    a real backend.

    Verb choice: the cookie path admits POST (JSON-RPC request/
    response). GET is gated to bearer-only callers since v5.0.72 —
    the backend's ``_handle_get`` derives ``agent_id`` from the
    bearer for ``session_registry`` fan-out and cookie-only GETs
    have no derivable agent_id (verify-all-v4 MUTATING #2 follow-up).

    Concretely:

      * No cookie + no bearer → 401 (unchanged).
      * Valid cookie + project membership → request reaches the
        proxy. The backend isn't up in test, so we land on a 502/504,
        NOT a 401. The bearer-validation 401 envelope ("invalid or
        missing agent bearer token") would mean the cookie path
        didn't trigger.
      * Valid cookie + non-member → 401 from the cookie path.
    """
    import os
    import secrets

    from agent_mcp.router import project_orchestrator as _po

    register_project("delta")
    # F015 v4: the on-disk HMAC key is owned by the systemd unit's
    # ExecStartPre in production. Bypass systemd in this unit test
    # and write the key directly so ``_forwarding_header_from_cookie``
    # has bytes to sign with. Skipping the real systemctl start keeps
    # the test focused on the cookie auth-gate decision (the 5xx
    # after the gate is still observed below).
    key_path = _po._forwarding_hmac_path("delta")
    key_path.write_bytes(secrets.token_bytes(32))
    os.chmod(key_path, 0o600)
    assert _po.ensure_forwarding_hmac_key("delta") is not None
    # Seed two operators. The conftest's register_project already
    # seeded the sentinel operator (so alice/bob are NOT the first
    # user) — alice gets an explicit membership grant; bob stays
    # outside ``project_membership`` to exercise the negative path.
    alice_id = _seed_user("alice")
    _seed_user("bob")
    _identity_module().add_project_membership(alice_id, "delta")
    client = await aiohttp_client(router_app)

    _MCP_BODY = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    _MCP_HEADERS = {"Content-Type": "application/json"}

    # No auth at all → bearer-validation 401.
    resp = await client.post(
        "/agent-mcp/mcp/delta",
        data=_MCP_BODY,
        headers=_MCP_HEADERS,
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()

    # Member cookie → request flows past the auth gate. The proxy
    # then fails to reach the (un-spawned) backend with a 5xx — that
    # proves the cookie path matched and we did NOT 401 here.
    alice_cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/mcp/delta",
        data=_MCP_BODY,
        headers=_MCP_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert resp.status != 401, (
        f"valid operator cookie should reach the proxy, got 401: "
        f"{await resp.text()}"
    )

    # Non-member cookie → 401 from the cookie path inside
    # backend_mcp_handler (bob has no project_membership row).
    bob_cookie = await _login(client, "bob")
    resp = await client.post(
        "/agent-mcp/mcp/delta",
        data=_MCP_BODY,
        headers=_MCP_HEADERS,
        cookies={"agent_mcp_session": bob_cookie},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


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


# ── Browser HTML redirect vs API JSON 401 ──────────────────────────
#
# Reproducer for the user-reported "i am still getting logibg errors"
# UX gap (2026-06-26): a browser user with no session hitting
# https://nixos-developer-system.tailfdae0.ts.net/agent-mcp got a raw
# JSON envelope splattered into the viewport instead of being bounced
# to the login form:
#
#   $ curl -sS .../agent-mcp -i
#   HTTP/2 401
#   content-type: application/json; charset=utf-8
#   {"error": "login_required", "message": "session cookie missing or
#    invalid", "login_url": "/agent-mcp/login"}
#
# The JSON does carry the right ``login_url`` field, but browsers
# don't auto-follow a hint inside a JSON body — only fetch-based
# clients do. The fix: content-negotiate the 401 — emit an HTML 303
# to ``/agent-mcp/login?next=<orig>`` when ``Accept: text/html`` is
# present, keep the 401 JSON envelope for API callers.


async def test_no_cookie_html_request_redirects_to_login(
    aiohttp_client, router_app,
) -> None:
    """A browser-shaped GET (``Accept: text/html,*/*;q=0.8``) to the
    dashboard root with no session cookie redirects to the login
    form instead of returning JSON.

    Reproduces the user-reported "i am still getting logibg errors"
    UX gap where the raw JSON 401 envelope was being rendered into
    the browser viewport.
    """
    _seed_user("alice")
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/",
        headers={"Accept": "text/html,*/*;q=0.8"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert resp.headers["Location"] == "/agent-mcp/login?next=/agent-mcp/"


async def test_no_cookie_html_request_preserves_path_and_query(
    aiohttp_client, router_app,
) -> None:
    """A browser deep-link with a query string redirects to login
    with the original path + query URL-encoded into ``?next=`` so the
    operator lands back on the same view after authenticating.

    The login handler's ``_safe_next`` (login.py) already constrains
    the next target to ``/agent-mcp/...``, so the redirect is safe
    even though the path is operator-supplied.
    """
    _seed_user("alice")
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/app/washing-brothers/?page=memories",
        headers={"Accept": "text/html,*/*"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert resp.headers["Location"] == (
        "/agent-mcp/login"
        "?next=/agent-mcp/app/washing-brothers/%3Fpage%3Dmemories"
    )


async def test_no_cookie_json_api_request_still_returns_401_json(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression guard: API clients that ask for JSON still get the
    401 JSON envelope. The dashboard's ApiClient depends on the
    ``error: "login_required"`` discriminator to drive its own
    in-browser redirect; flipping ALL 401s to 303s would break it.
    """
    register_project("alpha")
    _seed_user("alice")
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/alpha/memories",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()
    body = await resp.json()
    assert body.get("error") == "login_required"


async def test_no_cookie_no_accept_header_returns_401_json(
    aiohttp_client, router_app,
) -> None:
    """Non-browser tooling without an explicit ``Accept`` header (or
    with ``*/*`` only) falls through to the legacy 401 JSON path.

    Safe default: only redirect when the caller has explicitly asked
    for HTML.
    """
    _seed_user("alice")
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/app/",
        headers={"Accept": "*/*"},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()
    body = await resp.json()
    assert body.get("error") == "login_required"
