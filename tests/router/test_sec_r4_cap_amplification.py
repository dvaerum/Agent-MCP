"""Security round 4: capability-grant / group-join privilege amplification.

Two findings of one root class — a delegated operator amplifying their
own authority beyond what they hold, exploiting a management cap that
was only ever meant to let them ADMINISTER grants, not MINT new ones for
themselves.

Finding AZ-1 [HIGH] — capability-grant amplification (confused deputy).
    ``PUT /api/router/groups/<gid>/capabilities`` is gated only by
    ``system.groups.capabilities.manage``. The handler validated each
    requested cap against ``KNOWN_CAPABILITIES`` but never intersected
    the request with the CALLER's own cap set, so a non-sysadmin holding
    just that one management cap could grant their own group the
    sysadmin-equivalent ``system.*`` management caps
    (``system.users.manage`` etc.) and self-amplify. Fix: a non-sysadmin
    may only grant capabilities they themselves already hold; a sysadmin
    may grant anything in ``KNOWN_CAPABILITIES``.

Finding AZ-2 [MED] — join-a-high-capability-group amplification.
    ``POST /api/router/groups/<gid>/members`` (gated
    ``system.groups.manage``) blocked adding a member into a
    sysadmin-FLAGGED group but not into a group merely carrying elevated
    ``system.*`` capabilities — an independent amplification path (add
    yourself / a group you control, inherit the caps). Fix: same guard —
    a non-sysadmin may not add a member into a group whose RESOLVED caps
    include any cap the caller does not themselves hold.

Both tests drive the real middleware + route stack, so the seam asserted
is the wired-up auth/handler seam, not an in-process helper.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror test_sec_sysadmin_escalation.py) ───────────────


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
    """Create a user. The first-ever user is auto-promoted to sysadmin by
    the router bootstrap, so seed a throwaway sentinel sysadmin first when
    the table is empty to keep the real test user at ``is_sysadmin=0``."""
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


def _seed_group(group_id: str, name: str) -> str:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, 0, '2026-06-30T00:00:00')",
            (group_id, name),
        )
    return group_id


def _grant_capability(group_id: str, *caps: str) -> None:
    from agent_mcp.repositories import group_capability_repository

    group_capability_repository.replace(group_id, caps)


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


async def _delegated_client(aiohttp_client, router_app, *caps: str):
    """Log in a non-sysadmin operator 'alice' who carries ``caps`` via a
    group capability grant. Returns (client, cookie)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie


# ── AZ-1: capability-grant amplification ───────────────────────────


async def test_delegated_caps_manager_cannot_grant_unheld_cap(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin holding only ``system.groups.capabilities.manage``
    must NOT be able to PUT a cap they don't hold (``system.users.manage``)
    onto a group — that's the self-amplification vector. Expect 403."""
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.capabilities.manage",
    )
    _seed_group("g-target", "target")

    resp = await client.put(
        "/agent-mcp/api/router/groups/g-target/capabilities",
        data=json.dumps({"capabilities": ["system.users.manage"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


async def test_sysadmin_can_grant_any_cap(
    aiohttp_client, router_app,
) -> None:
    """A sysadmin may grant any KNOWN capability — the amplification guard
    must not over-reject the legitimate sysadmin path."""
    _seed_user("root", is_sysadmin=True)
    _seed_group("g-sys-target", "sys-target")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.put(
        "/agent-mcp/api/router/groups/g-sys-target/capabilities",
        data=json.dumps({"capabilities": ["system.users.manage"]}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert "system.users.manage" in (await resp.json())["capabilities"]


async def test_delegated_caps_manager_can_grant_held_cap(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin may grant a capability they THEMSELVES hold — the
    guard only blocks amplification, not legitimate delegation."""
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.capabilities.manage",
    )
    _seed_group("g-held", "held")

    resp = await client.put(
        "/agent-mcp/api/router/groups/g-held/capabilities",
        data=json.dumps(
            {"capabilities": ["system.groups.capabilities.manage"]}
        ),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert (
        "system.groups.capabilities.manage"
        in (await resp.json())["capabilities"]
    )


# ── AZ-2: join-a-high-capability-group amplification ───────────────


async def test_delegated_group_manager_cannot_join_high_cap_group(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin with ``system.groups.manage`` must NOT be able to
    add a member into a group carrying a ``system.*`` cap they don't hold
    (``system.users.manage``) — the member would inherit it. Expect 403."""
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("g-highcap", "high-cap")
    _grant_capability("g-highcap", "system.users.manage")
    member_id = _seed_user("newbie", is_sysadmin=False)

    resp = await client.post(
        "/agent-mcp/api/router/groups/g-highcap/members",
        data=json.dumps({"user_id": member_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


async def test_delegated_group_manager_can_join_held_cap_group(
    aiohttp_client, router_app,
) -> None:
    """Regression guard: adding a member into a group whose resolved caps
    are all held by the caller still succeeds — the guard only blocks
    amplification beyond the caller's own authority."""
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("g-lowcap", "low-cap")
    _grant_capability("g-lowcap", "system.groups.manage")
    member_id = _seed_user("newbie2", is_sysadmin=False)

    resp = await client.post(
        "/agent-mcp/api/router/groups/g-lowcap/members",
        data=json.dumps({"user_id": member_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 201, await resp.text()


async def test_sysadmin_can_add_member_to_high_cap_group(
    aiohttp_client, router_app,
) -> None:
    """A sysadmin may add members into any group — the AZ-2 guard must not
    over-reject the legitimate sysadmin path."""
    _seed_user("root", is_sysadmin=True)
    _seed_group("g-sys-highcap", "sys-high-cap")
    _grant_capability("g-sys-highcap", "system.users.manage")
    member_id = _seed_user("newbie3", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.post(
        "/agent-mcp/api/router/groups/g-sys-highcap/members",
        data=json.dumps({"user_id": member_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 201, await resp.text()
