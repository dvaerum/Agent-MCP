"""REST API for per-group capability grants — Wave 9 PR 5 (prancy-napping-pie).

Two new endpoints managed by the sysadmin via the Wave 9 dashboard
Capabilities panel:

    GET /agent-mcp/api/router/groups/<group_id>/capabilities
    PUT /agent-mcp/api/router/groups/<group_id>/capabilities
        body: {"capabilities": ["tasks.create", ...]}

Both routes are sysadmin-only. The cap
``system.groups.capabilities.manage`` is granted ONLY to the
sysadmin set per the Wave 9 bundle table; until the Wave 9 PR 4
aiohttp-shaped capability decorator lands, the route wrapping uses
``require_sysadmin`` (functionally equivalent for this cap).

What this module pins:

* Auth gate — sysadmin admits, non-sysadmin gets 403, no session
  gets 401.
* Round-trip — PUT then GET returns the same set, regardless of
  caller-supplied list order and de-duplication semantics.
* Unknown cap rejection — PUT containing a string not in
  :data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES` returns
  400 with the discriminated ``unknown_capability`` error code so
  the dashboard can render "did you typo it?".
* 404 — operating on an unknown group_id returns 404 (not 200 with
  an empty list, which would mask "I sent the wrong id" bugs).
* Empty body — PUT with ``{"capabilities": []}`` clears the row
  for the group.
"""

from __future__ import annotations

import json

import pytest


# Each test seeds its own user and logs in explicitly. The auto-login
# sentinel-session fixture is fine for the legacy CRUD tests because
# the sentinel IS the bootstrap sysadmin, but the auth gate test below
# needs a non-sysadmin caller, so we opt out at module scope and have
# the sysadmin tests log in their own sysadmin too — keeps the auth
# story consistent across the module.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror test_p3_perm_overhaul.py shape) ────────────────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(
    username: str,
    password: str = "passwordpassword",
    *,
    is_sysadmin: bool = False,
) -> str:
    """Create a user; optionally promote to sysadmin. Always make sure
    SOMEBODY else grabbed the first-user slot so the test user can
    land as non-sysadmin without the implicit promotion."""
    identity = _identity_module()
    with identity._connect() as conn:
        is_empty = (
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        )
    if is_empty and username != "__test_first_sysadmin":
        identity.create_user(
            username="__test_first_sysadmin",
            password="ignoredsentinelpassword",
        )
    user_id = identity.create_user(username=username, password=password)
    if is_sysadmin:
        with identity._connect() as conn:
            conn.execute(
                "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
                (user_id,),
            )
    return user_id


async def _login(client, username: str, password: str = "passwordpassword") -> str:
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie, "expected Set-Cookie on successful login"
    name_val = set_cookie.split(";", 1)[0]
    _, _, value = name_val.partition("=")
    return value.strip()


async def _create_group_as_sysadmin(
    client, cookie: str, name: str,
) -> str:
    """POST a group via the sysadmin session; return the new group_id."""
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": name}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 201, await resp.text()
    return (await resp.json())["group"]["group_id"]


# ── Auth gate ──────────────────────────────────────────────────────


async def test_get_requires_session(
    aiohttp_client, router_app,
) -> None:
    """No session cookie → 401 from the auth middleware. The route
    itself never runs."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/groups/anyid/capabilities",
        headers=_REST_HEADERS,
        allow_redirects=False,
    )

    assert resp.status == 401, await resp.text()


async def test_get_admits_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """A sysadmin caller passes ``require_sysadmin``; an existing
    group's caps GET returns 200 with an empty list (default state).
    """
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")

    resp = await client.get(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["capabilities"] == []


async def test_get_rejects_non_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """The cap ``system.groups.capabilities.manage`` is sysadmin-only;
    a logged-in operator who isn't a sysadmin gets 403 (not 401)."""
    # Sysadmin creates the group, then a non-sysadmin tries to read it.
    _seed_user("root", is_sysadmin=True)
    _seed_user("alice", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    root_cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, root_cookie, "ops")

    alice_cookie = await _login(client, "alice")
    resp = await client.get(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()


async def test_put_rejects_non_sysadmin(
    aiohttp_client, router_app,
) -> None:
    _seed_user("root", is_sysadmin=True)
    _seed_user("alice", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    root_cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, root_cookie, "ops")

    alice_cookie = await _login(client, "alice")
    resp = await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": ["tasks.create"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()


# ── Round-trip (PUT then GET returns same set) ─────────────────────


async def test_put_then_get_round_trip(
    aiohttp_client, router_app,
) -> None:
    """PUT a cap list, GET it back, assert membership equality.

    The handler sorts the GET response alphabetically — the dashboard
    relies on a stable render order — so the assertion uses set
    equality on the values, and a separate assertion on sort order.
    """
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")

    payload_caps = ["tasks.create", "agents.view", "messages.send"]
    put_resp = await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": payload_caps}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert put_resp.status == 200, await put_resp.text()
    put_body = await put_resp.json()
    assert put_body["success"] is True
    assert set(put_body["capabilities"]) == set(payload_caps)

    get_resp = await client.get(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert get_resp.status == 200, await get_resp.text()
    get_body = await get_resp.json()
    assert set(get_body["capabilities"]) == set(payload_caps)
    # Stable sort contract: the dashboard renders these in order.
    assert get_body["capabilities"] == sorted(get_body["capabilities"])


async def test_put_replaces_existing_set(
    aiohttp_client, router_app,
) -> None:
    """Two consecutive PUTs reflect the SECOND list, not the union —
    the contract is "set to exactly these", not "additive grant"."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")

    await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": ["tasks.create", "tasks.view"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": ["agents.view"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    resp = await client.get(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200
    assert (await resp.json())["capabilities"] == ["agents.view"]


async def test_put_empty_list_clears_caps(
    aiohttp_client, router_app,
) -> None:
    """``{"capabilities": []}`` is the "this group has no extra caps"
    state. The handler must accept it and the GET must return [].
    """
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")
    # Populate first so the clear is observable.
    await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": ["tasks.view"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    clear_resp = await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": []}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert clear_resp.status == 200, await clear_resp.text()
    assert (await clear_resp.json())["capabilities"] == []


# ── Validation: unknown cap, malformed body ────────────────────────


async def test_put_rejects_unknown_capability(
    aiohttp_client, router_app,
) -> None:
    """A cap string not in KNOWN_CAPABILITIES gets a 400 with the
    discriminated ``unknown_capability`` error code so the dashboard
    can render "did you typo it?". Uses ``agents.creat`` — a typo'd
    ``agents.create`` — exactly per the Wave 9 PR 5 plan."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")

    resp = await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": ["agents.creat"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 400, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "unknown_capability"
    assert "agents.creat" in body["unknown"]


async def test_put_unknown_cap_leaves_existing_intact(
    aiohttp_client, router_app,
) -> None:
    """Validation runs BEFORE the DB write; a rejected PUT must NOT
    clobber the existing cap set. Belt-and-braces against a future
    refactor that drops validation order."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")
    # Set a known good list first.
    await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": ["tasks.view", "agents.view"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    # Try a write that includes an unknown cap — must fail.
    bad = await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({
            "capabilities": ["tasks.create", "nonsense.cap"],
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert bad.status == 400, await bad.text()
    # Existing caps survive untouched.
    resp = await client.get(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200
    assert set((await resp.json())["capabilities"]) == {
        "tasks.view", "agents.view",
    }


async def test_put_rejects_non_list_body(
    aiohttp_client, router_app,
) -> None:
    """``{"capabilities": "tasks.create"}`` (a string instead of a
    list) gets a 400 validation_error. Defensive against operators
    hand-crafting requests."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")

    resp = await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": "tasks.create"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 400, await resp.text()
    body = await resp.json()
    assert body["error"] == "validation_error"


async def test_put_de_duplicates_caller_input(
    aiohttp_client, router_app,
) -> None:
    """``{"capabilities": ["tasks.view", "tasks.view"]}`` is accepted;
    the GET response shows only one copy. Matches the repository's
    de-dup contract."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")

    resp = await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": ["tasks.view", "tasks.view"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["capabilities"] == ["tasks.view"]


# ── Not-found ──────────────────────────────────────────────────────


async def test_get_unknown_group_returns_404(
    aiohttp_client, router_app,
) -> None:
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.get(
        "/agent-mcp/api/router/groups/no-such-group/capabilities",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 404, await resp.text()


async def test_put_unknown_group_returns_404(
    aiohttp_client, router_app,
) -> None:
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.put(
        "/agent-mcp/api/router/groups/no-such-group/capabilities",
        data=json.dumps({"capabilities": ["tasks.view"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 404, await resp.text()


# ── Cap surface integration: KNOWN_CAPABILITIES coverage ───────────


async def test_every_known_capability_round_trips(
    aiohttp_client, router_app,
) -> None:
    """A single PUT carrying every member of ``KNOWN_CAPABILITIES``
    succeeds, and the GET returns the same set. Catches the
    "validation rejects a real cap" class of regression — if the
    KNOWN set ever drifts from what the handler accepts, this test
    fires immediately."""
    from agent_mcp.core.capabilities import KNOWN_CAPABILITIES

    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    gid = await _create_group_as_sysadmin(client, cookie, "ops")

    caps = sorted(KNOWN_CAPABILITIES)
    resp = await client.put(
        f"/agent-mcp/api/router/groups/{gid}/capabilities",
        data=json.dumps({"capabilities": caps}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    assert set((await resp.json())["capabilities"]) == set(caps)
