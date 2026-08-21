"""R6-F2 (HIGH, live-exploited) — stale-Principal TOCTOU on the 8
body-bearing router admin handlers.

``require_operator_session_middleware`` (auth_middleware.py) resolves
the caller's Principal (sysadmin flag + capability set) ONCE, before
the handler's genuine ``await req.read()`` yield point inside
``admin_users_api._json_body``. A caller who paces body delivery (a
slow-drip POST/PATCH) can hold that read open while a concurrent
request revokes their privilege in the DB — the paused handler then
resumes and completes its write against the PRE-revocation snapshot
cached at entry.

Live-exploited repro (pentest-loop R6-F2): operator ``dev`` (sysadmin)
opens ``PATCH /users/{victim} {is_sysadmin: true}`` over a raw socket,
sends 1 body byte then pauses; during the pause a second session
demotes ``dev.is_sysadmin`` to ``False`` (200, committed); the paused
request then completes its body read and resumes → 200, minting a new
sysadmin off ``dev``'s stale pre-revocation Principal.

Fix: ``perm_gates.revalidate_capability_or_403`` re-resolves the
caller's Principal from a LIVE DB read immediately after ``_json_body``
returns and refreshes ``req['principal']`` / ``req['is_sysadmin']`` in
place — mirrors R5-F1's stream-revalidation pattern (re-run the live
gate right before the thing that matters, not just once at entry).

The tests below reproduce the race DETERMINISTICALLY — no real sleeps.
A monkeypatched ``_json_body`` pauses mid-read on an ``asyncio.Event``;
a concurrent revocation commits while it's paused; the read is then
released and the handler resumes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror tests/router/test_sec_r2_admin_users.py) ───────


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
    """Create a user. The first-ever user is auto-promoted to sysadmin
    by the router bootstrap, so seed a throwaway sentinel sysadmin
    first when the table is empty to keep the real test user at
    ``is_sysadmin=0`` by default."""
    identity = _identity_module()
    with identity._connect() as conn:
        is_empty = conn.execute(
            "SELECT COUNT(*) AS n FROM users"
        ).fetchone()["n"] == 0
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
    name, _, value = name_val.partition("=")
    assert name.strip() == "agent_mcp_session"
    return value.strip()


def _sysadmin_count() -> int:
    identity = _identity_module()
    with identity._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_sysadmin = 1"
        ).fetchone()["n"]


# ── Test A: the LIVE-EXPLOITED repro — edit_user_handler ────────────


@pytest.mark.no_auth_seed_session
async def test_slow_drip_edit_user_rejects_stale_sysadmin_grant(
    aiohttp_client, router_app, monkeypatch,
) -> None:
    """A slow-drip PATCH granting ``is_sysadmin`` must re-check the
    caller's LIVE privilege after the body-read yield point, not the
    entry-time snapshot.

    ``dev`` (sysadmin) opens the grant on ``victim``; while dev's
    body-read is paused, ``root2`` (also sysadmin) demotes dev — the
    demotion COMMITS before dev's request resumes. Pre-fix this still
    200s (dev's stale snapshot survives the revocation); post-fix it
    must 403 and ``victim`` must stay a non-sysadmin.
    """
    from agent_mcp.router import admin_users_api

    dev_id = _seed_user("dev", is_sysadmin=True)
    _seed_user("root2", is_sysadmin=True)
    victim_id = _seed_user("victim", is_sysadmin=False)
    # NOTE: ``_seed_user`` auto-seeds a throwaway "__test_first_sysadmin"
    # the first time the (fresh, per-test) DB is empty — so the count
    # here is 3 (sentinel + dev + root2), not 2. It stays sysadmin
    # throughout, which is exactly what keeps the demotion below legal
    # (never drops the GLOBAL count to zero).
    before_count = _sysadmin_count()
    assert before_count == 3

    client = await aiohttp_client(router_app)
    dev_cookie = await _login(client, "dev")
    admin_client = await aiohttp_client(router_app)
    root2_cookie = await _login(admin_client, "root2")

    body_read_started = asyncio.Event()
    release_body_read = asyncio.Event()
    original_json_body = admin_users_api._json_body

    async def paused_json_body(req):
        # Only pause the ATTACK request (PATCH on the victim); the
        # concurrent demotion PATCH (on dev) must sail through
        # unpaused or the race can't be set up deterministically.
        if req.match_info.get("user_id") == victim_id:
            body_read_started.set()
            await release_body_read.wait()
        return await original_json_body(req)

    monkeypatch.setattr(admin_users_api, "_json_body", paused_json_body)

    async def _attack():
        return await client.patch(
            f"/agent-mcp/api/router/users/{victim_id}",
            data=json.dumps({"is_sysadmin": True}),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": dev_cookie},
            allow_redirects=False,
        )

    attack_task = asyncio.ensure_future(_attack())
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    # Concurrent LEGITIMATE revocation commits to the DB while the
    # attack's body-read is paused mid-flight.
    demote_resp = await admin_client.patch(
        f"/agent-mcp/api/router/users/{dev_id}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": root2_cookie},
        allow_redirects=False,
    )
    assert demote_resp.status == 200, await demote_resp.text()
    assert _sysadmin_count() == before_count - 1  # dev demoted

    release_body_read.set()
    resp = await asyncio.wait_for(attack_task, timeout=5)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    row = _identity_module().get_user_by_username("victim")
    assert not row["is_sysadmin"], (
        "victim must NOT have been promoted off dev's stale, "
        "pre-revocation Principal snapshot"
    )
    assert _sysadmin_count() == before_count - 1


# ── Test B: second wiring of the same race — create_group_handler ──


@pytest.mark.no_auth_seed_session
async def test_slow_drip_create_sysadmin_group_rejects_stale_grant(
    aiohttp_client, router_app, monkeypatch,
) -> None:
    """Same race, different handler: a caller demoted mid-flight must
    not mint a sysadmin-flagged GROUP off their stale snapshot either
    (``create_group_handler`` is a separate ``_json_body`` call site
    from ``edit_user_handler``)."""
    from agent_mcp.router import admin_users_api

    dev_id = _seed_user("dev2", is_sysadmin=True)
    _seed_user("root3", is_sysadmin=True)
    # See test_slow_drip_edit_user_rejects_stale_sysadmin_grant's NOTE:
    # ``_seed_user`` auto-seeds a throwaway sentinel sysadmin too, so the
    # count is 3 (sentinel + dev2 + root3), not 2.
    assert _sysadmin_count() == 3

    client = await aiohttp_client(router_app)
    dev_cookie = await _login(client, "dev2")
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root3")

    body_read_started = asyncio.Event()
    release_body_read = asyncio.Event()
    original_json_body = admin_users_api._json_body

    target_group_name = "race-sysadmin-group"

    async def paused_json_body(req):
        # create_group_handler is a POST to the bare collection route
        # (no match_info id to key on) — key on path + method instead.
        if req.path.endswith("/api/router/groups") and req.method == "POST":
            body_read_started.set()
            await release_body_read.wait()
        return await original_json_body(req)

    monkeypatch.setattr(admin_users_api, "_json_body", paused_json_body)

    async def _attack():
        return await client.post(
            "/agent-mcp/api/router/groups",
            data=json.dumps({"name": target_group_name, "is_sysadmin": True}),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": dev_cookie},
            allow_redirects=False,
        )

    attack_task = asyncio.ensure_future(_attack())
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    demote_resp = await admin_client.patch(
        f"/agent-mcp/api/router/users/{dev_id}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": root_cookie},
        allow_redirects=False,
    )
    assert demote_resp.status == 200, await demote_resp.text()

    release_body_read.set()
    resp = await asyncio.wait_for(attack_task, timeout=5)

    assert resp.status == 403, await resp.text()
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM groups WHERE name = ?", (target_group_name,),
        ).fetchone()
    assert row is None, (
        "sysadmin-flagged group must NOT have been created off dev's "
        "stale, pre-revocation Principal snapshot"
    )


# ── Test C: the shared helper is wired into all 8 handlers ─────────


async def test_revalidate_helper_invoked_at_all_eight_sites(
    aiohttp_client, router_app, register_project, monkeypatch,
) -> None:
    """Coverage + non-regression: every one of the 8 body-bearing admin
    handlers calls ``revalidate_capability_or_403`` exactly once, with
    the SAME capability string its ``require_capability`` route gate
    uses — and the happy path (no concurrent revocation) still
    completes exactly as before the fix.
    """
    from agent_mcp.router import perm_gates

    calls: list[str] = []
    original = perm_gates.revalidate_capability_or_403

    async def spy(req, cap):
        calls.append(cap)
        return await original(req, cap)

    monkeypatch.setattr(perm_gates, "revalidate_capability_or_403", spy)

    # Default aiohttp_client auto-logs in the sentinel sysadmin.
    client = await aiohttp_client(router_app)

    # 1. create_user_handler
    create_user = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps(
            {"username": "spyuser1", "password": "spyuserpassword1"}
        ),
        headers=_REST_HEADERS,
    )
    assert create_user.status == 201, await create_user.text()
    user_id = (await create_user.json())["user"]["user_id"]

    # 2. edit_user_handler
    edit_user = await client.patch(
        f"/agent-mcp/api/router/users/{user_id}",
        data=json.dumps({"email": "spy@example.test"}),
        headers=_REST_HEADERS,
    )
    assert edit_user.status == 200, await edit_user.text()

    # 3. create_group_handler
    create_group = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "spygroup1"}),
        headers=_REST_HEADERS,
    )
    assert create_group.status == 201, await create_group.text()
    group_id = (await create_group.json())["group"]["group_id"]

    # 4. edit_group_handler
    edit_group = await client.patch(
        f"/agent-mcp/api/router/groups/{group_id}",
        data=json.dumps({"name": "spygroup1-renamed"}),
        headers=_REST_HEADERS,
    )
    assert edit_group.status == 200, await edit_group.text()

    # 5. add_group_member_handler
    add_member = await client.post(
        f"/agent-mcp/api/router/groups/{group_id}/members",
        data=json.dumps({"user_id": user_id}),
        headers=_REST_HEADERS,
    )
    assert add_member.status == 201, await add_member.text()

    # 6. add_project_membership_handler
    register_project("spyproject")
    add_membership = await client.post(
        "/agent-mcp/api/router/projects/spyproject/memberships",
        data=json.dumps({"user_id": user_id, "role": "viewer"}),
        headers=_REST_HEADERS,
    )
    assert add_membership.status == 201, await add_membership.text()

    # 7. change_project_membership_role_handler
    change_role = await client.patch(
        f"/agent-mcp/api/router/projects/spyproject/memberships/u:{user_id}",
        data=json.dumps({"role": "operator"}),
        headers=_REST_HEADERS,
    )
    assert change_role.status == 200, await change_role.text()

    # 8. replace_group_capabilities_handler
    replace_caps = await client.put(
        f"/agent-mcp/api/router/groups/{group_id}/capabilities",
        data=json.dumps({"capabilities": ["system.users.manage"]}),
        headers=_REST_HEADERS,
    )
    assert replace_caps.status == 200, await replace_caps.text()

    assert calls == [
        "system.users.manage",  # create_user_handler
        "system.users.manage",  # edit_user_handler
        "system.groups.manage",  # create_group_handler
        "system.groups.manage",  # edit_group_handler
        "system.groups.manage",  # add_group_member_handler
        "system.projects.manage",  # add_project_membership_handler
        "system.projects.manage",  # change_project_membership_role_handler
        "system.groups.capabilities.manage",  # replace_group_capabilities_handler
    ], calls


# ── Test D: happy-path regression — no false-positive 403s ─────────


@pytest.mark.no_auth_seed_session
async def test_non_racing_sysadmin_grant_still_succeeds(
    aiohttp_client, router_app,
) -> None:
    """Regression: a normal (non-racing) request from a caller whose
    privilege is NOT revoked mid-flight must still succeed exactly as
    before the fix — the revalidation must not spuriously reject a
    legitimate, uncontested grant."""
    _seed_user("stabledev", is_sysadmin=True)
    victim_id = _seed_user("stablevictim", is_sysadmin=False)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "stabledev")

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{victim_id}",
        data=json.dumps({"is_sysadmin": True}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["user"]["is_sysadmin"] is True
    row = _identity_module().get_user_by_username("stablevictim")
    assert row["is_sysadmin"]
