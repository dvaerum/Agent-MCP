"""Security round 12: revoke-side authority amplification (AZ-R12-1 [MED]).

The REVOKE mirror of the already-fixed GRANT-side amplification guards.
Three router-admin revoke handlers manipulate authority a non-sysadmin
delegate could never GRANT, yet were UNGUARDED — so a delegate holding a
``system.*.manage`` cap could STRIP authority (project data access,
group-conferred caps, group membership) from others across tenants:

  1. ``DELETE /api/router/projects/<name>/memberships/<mid>``
     (gated ``system.projects.manage``) — revoking a project-membership
     row the delegate has NO authority over. The ADD sibling is guarded
     by ``_membership_grant_denied``; the DELETE path was not.

  2. ``DELETE /api/router/groups/<gid>/members/<mid>``
     (gated ``system.groups.manage``) — removing a member from a group
     that confers sysadmin / elevated caps / project roles the delegate
     lacks. The ADD sibling (``add_group_member_handler``) is guarded;
     the remove path was not.

  3. ``PUT /api/router/groups/<gid>/capabilities``
     (gated ``system.groups.capabilities.manage``) — the grant guard
     only inspected the NEW cap list, so a delegate could PUT a
     SHRINKING set (or ``[]``) to STRIP caps they do not themselves
     hold. The guard must cover the symmetric difference (caps added
     ∪ caps removed).

Fix: mirror the grant-side guards onto the revoke direction. A
non-sysadmin may revoke only authority they could themselves grant;
sysadmins keep full behaviour.

These tests drive the real middleware + route stack, so the seam
asserted is the wired-up auth/handler seam, not an in-process helper.
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

_PROJ = "proj-r12"


# ── Helpers (mirror test_sec_r6_groupjoin_membership.py) ───────────


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


def _seed_project_membership(
    project_name: str,
    *,
    user_id: str | None = None,
    group_id: str | None = None,
    role: str = "operator",
) -> None:
    """Insert a project_membership row directly (role-explicit)."""
    identity = _identity_module()
    with identity._connect() as conn:
        if user_id is not None:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, role) VALUES (?, ?, ?)",
                (project_name, user_id, role),
            )
        else:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, group_id, role) "
                "VALUES (?, NULL, ?, ?)",
                (project_name, group_id, role),
            )


async def _login(
    client, username: str, password: str = "passwordpassword",
) -> str:
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
    group capability grant. Returns (client, cookie, alice_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id


# ══ Instance 1 — DELETE project membership ═════════════════════════
#    Mirror of add_project_membership_handler's _membership_grant_denied.


async def test_delegate_cannot_revoke_project_membership_with_no_role(
    aiohttp_client, router_app, register_project,
) -> None:
    """AZ-R12-1 #1: a non-sysadmin holding only ``system.projects.manage``
    with NO membership on ``proj-r12`` must NOT be able to DELETE a victim's
    operator membership — that is cross-tenant revoke of data access the
    delegate has no authority over.

    R7-F1: the non-member response is now a uniform 404 ``not_found`` (was
    403 ``forbidden``) so an existing-hidden project is indistinguishable
    from a nonexistent one (project-existence oracle closed). The revoke is
    still fully blocked — 404 denies it just as 403 did."""
    register_project(_PROJ)
    client, cookie, _alice = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")

    resp = await client.delete(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    # The revoke was blocked — the victim row survives.
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM project_membership "
            "WHERE project_name = ? AND user_id = ?",
            (_PROJ, victim_id),
        ).fetchone()
    assert row is not None


async def test_viewer_delegate_cannot_revoke_operator_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """RED (AZ-R12-1 #1): a VIEWER-role delegate may not revoke an OPERATOR
    membership — revoking a role above their own is out of their authority,
    exactly as they could not GRANT operator. Expect 403."""
    register_project(_PROJ)
    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="viewer")
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")

    resp = await client.delete(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


async def test_operator_delegate_can_revoke_viewer_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """GREEN regression: an operator-role delegate revoking a VIEWER
    membership (a role at or below their own) still succeeds — the guard
    only blocks revoking authority beyond the caller's own. Expect 200."""
    register_project(_PROJ)
    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="operator")
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="viewer")

    resp = await client.delete(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()


async def test_sysadmin_can_revoke_project_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """GREEN regression: a sysadmin may revoke any membership — the guard
    must not over-reject the legitimate sysadmin path. Expect 200."""
    register_project(_PROJ)
    _seed_user("root", is_sysadmin=True)
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.delete(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()


# ══ Instance 1b — PATCH downgrade (revoke via role change) ═════════
#    Discovered during the class-sweep: change_project_membership_role
#    guarded only the NEW role, so a downgrade STRIPS the old role
#    unchecked — a trivial bypass of the DELETE guard above.


async def test_viewer_delegate_cannot_downgrade_operator_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """RED (AZ-R12-1, 4th sibling): a VIEWER-role delegate PATCHing a
    victim's OPERATOR row down to viewer STRIPS the operator role — a
    revoke the delegate could never GRANT, and a bypass of the DELETE
    guard (downgrade-to-viewer ≈ lockout of operator-tier access). The old
    guard only inspected the new (viewer) role and allowed it. Expect 403.

    On origin/main (and with only the NEW-role guard) this returns 200."""
    register_project(_PROJ)
    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="viewer")
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")

    resp = await client.patch(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        data=json.dumps({"role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


async def test_operator_delegate_can_downgrade_operator_to_viewer(
    aiohttp_client, router_app, register_project,
) -> None:
    """GREEN regression: an OPERATOR-role delegate may downgrade another
    operator to viewer — they hold authority over BOTH the old and new
    role, so the change is within their own authority. Expect 200."""
    register_project(_PROJ)
    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="operator")
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")

    resp = await client.patch(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        data=json.dumps({"role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()


async def test_sysadmin_can_downgrade_operator_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """GREEN regression: a sysadmin may change any role — the old-role
    guard must not over-reject the legitimate sysadmin path. Expect 200."""
    register_project(_PROJ)
    _seed_user("root", is_sysadmin=True)
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.patch(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        data=json.dumps({"role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()


# ══ Instance 2 — DELETE group member ═══════════════════════════════
#    Mirror of add_group_member_handler's amplification guards.


async def test_delegate_cannot_remove_member_from_cap_conferring_group(
    aiohttp_client, router_app,
) -> None:
    """RED (AZ-R12-1 #2): a delegate holding ``system.groups.manage`` but
    NOT ``system.users.manage`` must NOT be able to remove a member from a
    group that confers ``system.users.manage`` — stripping a cap-conferring
    membership the delegate could never GRANT (the add-side blocks joining
    such a group). Expect 403.

    On origin/main the remove path is unguarded and returns 200."""
    from agent_mcp.router import group_resolver

    client, cookie, _alice = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("g-caps", "Cap Holders")
    _grant_capability("g-caps", "system.users.manage")
    victim_id = _seed_user("victim", is_sysadmin=False)
    group_resolver.add_group_member("g-caps", member_user_id=victim_id)

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/g-caps/members/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


async def test_delegate_cannot_remove_member_from_project_role_group(
    aiohttp_client, router_app, register_project,
) -> None:
    """RED (AZ-R12-1 #2): the project-role vector. A delegate with NO role
    on ``proj-r12`` may not remove a member from a group that is an
    operator-member of ``proj-r12`` — stripping the project role the
    delegate could never GRANT. Expect 403."""
    from agent_mcp.router import group_resolver

    register_project(_PROJ)
    client, cookie, _alice = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("g-proj", "Project Operators")
    _seed_project_membership(_PROJ, group_id="g-proj", role="operator")
    victim_id = _seed_user("victim", is_sysadmin=False)
    group_resolver.add_group_member("g-proj", member_user_id=victim_id)

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/g-proj/members/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


async def test_delegate_can_remove_member_from_held_cap_group(
    aiohttp_client, router_app,
) -> None:
    """GREEN regression: a delegate may remove a member from a group whose
    conferred caps are all caps the delegate ALSO holds — within their own
    authority to grant, so within their authority to revoke. Uses a
    ``system.*`` cap because ``has_capability`` only admits resource caps
    (``tasks.*``) with a project scope, which router-admin routes lack.
    Expect 200."""
    from agent_mcp.router import group_resolver

    client, cookie, _alice = await _delegated_client(
        aiohttp_client, router_app,
        "system.groups.manage", "system.users.manage",
    )
    _seed_group("g-safe", "Safe Team")
    _grant_capability("g-safe", "system.users.manage")
    victim_id = _seed_user("victim", is_sysadmin=False)
    group_resolver.add_group_member("g-safe", member_user_id=victim_id)

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/g-safe/members/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()


async def test_sysadmin_can_remove_member_from_cap_conferring_group(
    aiohttp_client, router_app,
) -> None:
    """GREEN regression: a sysadmin may remove any member — the guard must
    not over-reject the legitimate sysadmin path. Expect 200."""
    from agent_mcp.router import group_resolver

    _seed_user("root", is_sysadmin=True)
    _seed_group("g-caps", "Cap Holders")
    _grant_capability("g-caps", "system.users.manage")
    victim_id = _seed_user("victim", is_sysadmin=False)
    group_resolver.add_group_member("g-caps", member_user_id=victim_id)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/g-caps/members/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()


# ══ Instance 3 — PUT group capabilities (shrinking / strip) ════════
#    The grant guard only inspected the NEW list; cover the symmetric diff.


async def test_delegate_cannot_strip_unheld_cap_via_shrinking_put(
    aiohttp_client, router_app,
) -> None:
    """RED (AZ-R12-1 #3): a delegate holding
    ``system.groups.capabilities.manage`` but NOT ``system.users.manage``
    must NOT be able to PUT a SHRINKING cap list that REMOVES
    ``system.users.manage`` from a group that currently holds it — the
    grant guard only checked the new list, letting a delegate STRIP a cap
    they do not themselves hold. Expect 403.

    On origin/main the guard inspects only the (empty) new list → 200."""
    client, cookie, _alice = await _delegated_client(
        aiohttp_client, router_app, "system.groups.capabilities.manage",
    )
    _seed_group("g-target", "Target Group")
    _grant_capability("g-target", "system.users.manage")

    resp = await client.put(
        "/agent-mcp/api/router/groups/g-target/capabilities",
        data=json.dumps({"capabilities": []}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


async def test_delegate_can_strip_held_cap_via_shrinking_put(
    aiohttp_client, router_app,
) -> None:
    """GREEN regression: a delegate may PUT a shrinking list that removes a
    cap they THEMSELVES hold — revoking within their own authority. The
    delegate holds ``system.users.manage``; the target group holds it and
    the delegate strips it. A ``system.*`` cap is used because
    ``has_capability`` only admits resource caps with a project scope,
    which router-admin routes lack. Expect 200."""
    client, cookie, _alice = await _delegated_client(
        aiohttp_client, router_app,
        "system.groups.capabilities.manage", "system.users.manage",
    )
    _seed_group("g-target", "Target Group")
    _grant_capability("g-target", "system.users.manage")

    resp = await client.put(
        "/agent-mcp/api/router/groups/g-target/capabilities",
        data=json.dumps({"capabilities": []}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["capabilities"] == []


async def test_sysadmin_can_strip_any_cap_via_shrinking_put(
    aiohttp_client, router_app,
) -> None:
    """GREEN regression: a sysadmin may PUT any shrinking list — the
    symmetric-diff guard must not over-reject the legitimate sysadmin
    path. Expect 200."""
    _seed_user("root", is_sysadmin=True)
    _seed_group("g-target", "Target Group")
    _grant_capability("g-target", "system.users.manage")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.put(
        "/agent-mcp/api/router/groups/g-target/capabilities",
        data=json.dumps({"capabilities": []}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["capabilities"] == []
