"""Security round 6: group-join project-membership amplification + error hygiene.

Finding AZ-R6-1 [HIGH] — group-join confers inherited project_membership
    the caller lacks (the third amplification sibling of round 4's AZ-1/AZ-2
    and round 5's AZ-R5-1).

    ``POST /api/router/groups/<gid>/members`` (gated ``system.groups.manage``)
    already blocks a non-sysadmin from adding a member into a group that would
    confer sysadmin (the ``is_sysadmin`` flag) or elevated ``system.*``
    capabilities. But it did NOT check the parent group's ``project_membership``
    rows. ``group_resolver.resolve_user_project_role`` — the resolver the
    ``/api/<project>/`` data middleware gates on — inherits project roles
    through group membership. So joining a group that is a project member
    grants the joiner that project's role: a non-sysadmin holding only
    ``system.groups.manage`` could self-grant operator/viewer on ANY project
    that has a group as a member.

    Fix (mirrors round 5's ``_membership_grant_denied``): a non-sysadmin may
    only add a member (user OR nested group) into a group whose inherited
    project roles are all at or below the caller's OWN role on those projects,
    and only on projects where the caller holds a membership at all. Sysadmins
    keep full behaviour.

Finding SD-R6-2 [LOW] — raw ``sqlite3.IntegrityError`` reflected on error.
    ``add_group_member_handler`` reflected ``{e}`` on a non-UNIQUE
    IntegrityError (e.g. an FK violation), disclosing SQL-constraint/schema
    detail to the client. Fix: generic message.

These tests drive the real middleware + route stack, so the seam asserted is
the wired-up auth/handler seam, not an in-process helper.
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

_VICTIM = "r6victim"


# ── Helpers (mirror test_sec_r5_membership_self_escalation.py) ─────


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


# ── AZ-R6-1: block self-join into a project-member group (USER kind) ──


async def test_delegate_cannot_join_group_that_confers_unheld_project_role(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin holding only ``system.groups.manage`` with NO
    membership on ``r6victim`` must NOT be able to add THEMSELVES into a
    group that is an operator-member of ``r6victim`` — joining confers the
    project's operator role via the resolver the data middleware gates on.
    Expect 403."""
    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("r6team", "Victim Operators")
    _seed_project_membership(_VICTIM, group_id="r6team", role="operator")

    resp = await client.post(
        "/agent-mcp/api/router/groups/r6team/members",
        data=json.dumps({"user_id": alice_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


# ── AZ-R6-1: block nested-GROUP join (group-indirection) ──────────────


async def test_delegate_cannot_add_controlled_group_into_project_member_group(
    aiohttp_client, router_app,
) -> None:
    """Group-indirection must be guarded too: the delegate may not add a
    GROUP THEY CONTROL as a nested member of the operator-member group —
    the nested group (and anyone in it) would transitively inherit the
    project's operator role. Expect 403."""
    client, cookie, _alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("r6team", "Victim Operators")
    _seed_project_membership(_VICTIM, group_id="r6team", role="operator")
    _seed_group("g-controlled", "Alice Controlled")

    resp = await client.post(
        "/agent-mcp/api/router/groups/r6team/members",
        data=json.dumps({"group_id": "g-controlled"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


# ── AZ-R6-1: sysadmin keeps full behaviour ────────────────────────────


async def test_sysadmin_can_add_member_to_project_member_group(
    aiohttp_client, router_app,
) -> None:
    """A sysadmin may add members into a project-member group — the AZ-R6-1
    guard must not over-reject the legitimate sysadmin path."""
    _seed_user("root", is_sysadmin=True)
    _seed_group("r6team", "Victim Operators")
    _seed_project_membership(_VICTIM, group_id="r6team", role="operator")
    member_id = _seed_user("newbie", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.post(
        "/agent-mcp/api/router/groups/r6team/members",
        data=json.dumps({"user_id": member_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 201, await resp.text()


# ── AZ-R6-1: legitimate delegation (≤ own role) still allowed ─────────


async def test_operator_delegate_can_add_member_to_viewer_member_group(
    aiohttp_client, router_app,
) -> None:
    """Regression guard: a delegate who holds OPERATOR on ``r6victim`` may
    add a member into a group that is only a VIEWER-member of that project —
    conferring viewer is at or below their own role. Expect 201."""
    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_project_membership(_VICTIM, user_id=alice_id, role="operator")
    _seed_group("r6viewteam", "Victim Viewers")
    _seed_project_membership(_VICTIM, group_id="r6viewteam", role="viewer")
    member_id = _seed_user("newbie2", is_sysadmin=False)

    resp = await client.post(
        "/agent-mcp/api/router/groups/r6viewteam/members",
        data=json.dumps({"user_id": member_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 201, await resp.text()


# ── SD-R6-2: generic error on IntegrityError (no raw SQL leak) ─────────


async def test_integrity_error_returns_generic_message(
    aiohttp_client, router_app,
) -> None:
    """Adding a member with a non-existent ``user_id`` trips the FK
    constraint. The response must carry a GENERIC message, not the raw
    ``sqlite3.IntegrityError`` text (which discloses the SQL constraint /
    schema). Sysadmin caller so the amplification guards are skipped and we
    reach the INSERT/FK path cleanly."""
    _seed_user("root", is_sysadmin=True)
    _seed_group("r6team", "Team")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.post(
        "/agent-mcp/api/router/groups/r6team/members",
        data=json.dumps({"user_id": "no-such-user-id"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    body = await resp.json()
    assert body["success"] is False
    message = body.get("message", "")
    assert "FOREIGN KEY" not in message
    assert "constraint" not in message.lower()
    assert message == "could not add member"
