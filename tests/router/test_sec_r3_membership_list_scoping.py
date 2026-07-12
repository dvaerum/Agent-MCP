"""Security round 3 (pentest R3-F1): scope the project-memberships LIST to
members + close the project-existence oracle.

``GET /api/router/projects/<name>/memberships`` (``list_project_memberships_handler``)
was gated ONLY by the coarse deployment-wide ``system.projects.manage`` cap —
a DELEGABLE table-management authority. Its sibling MUTATIONS (add / change /
delete membership) additionally run ``_membership_grant_denied`` (AZ-R5-1), so
a non-sysadmin with no membership on the project "may confer nothing". Round
5's fix swept the three WRITE handlers but not the LIST — so:

  * a non-sysadmin delegate holding ``system.projects.manage`` via a group,
    with ZERO membership in project P, could ``GET …/projects/P/memberships``
    and receive **200 + the full roster** (user_ids, usernames, roles,
    membership_ids) of a project HIDDEN from their own ``/projects`` and
    ``/overview`` views — a cross-tenant disclosure; and
  * the 200-with-roster (real, not a member) vs 404 (nonexistent) differential
    was a project-existence oracle.

Fix (mirrors the mutation siblings + the auth-middleware non-member path,
PF-1): admit only a sysadmin OR a caller with a resolved role on the project;
otherwise return the SAME 404 ``unknown_project`` a non-member sees for a
nonexistent slug, so the two cases are indistinguishable.

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

_PROJ = "proj-secret"


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
    """Create a user. The first-ever user is auto-promoted to sysadmin by the
    router bootstrap, so seed a throwaway sentinel sysadmin first when the
    table is empty to keep the real test user at ``is_sysadmin=0``."""
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
    project: str,
    *,
    user_id: str | None = None,
    group_id: str | None = None,
    role: str = "operator",
) -> None:
    identity = _identity_module()
    with identity._connect() as conn:
        if user_id is not None:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, role) VALUES (?, ?, ?)",
                (project, user_id, role),
            )
        else:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, group_id, role) "
                "VALUES (?, NULL, ?, ?)",
                (project, group_id, role),
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
    """Log in a non-sysadmin operator 'alice' who carries ``caps`` via a group
    capability grant. Returns (client, cookie, alice_id, group_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id, group_id


# ── R3-F1: cross-tenant roster disclosure (the live exploit) ───────


async def test_delegate_without_membership_cannot_list_roster(
    aiohttp_client, router_app, register_project,
) -> None:
    """A non-sysadmin holding only ``system.projects.manage`` with NO
    membership on the project must NOT be able to read its roster — it returns
    404 ``unknown_project`` (was 200 + full roster)."""
    register_project(_PROJ)
    # A real member exists whose row the delegate must not be able to read.
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")

    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await client.get(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    # No roster leaked in the body.
    assert "memberships" not in body
    assert "victim" not in json.dumps(body)


# ── R3-F1: existence oracle closed ─────────────────────────────────


async def test_real_but_nonmember_indistinguishable_from_nonexistent(
    aiohttp_client, router_app, register_project,
) -> None:
    """The 200-roster / 404 differential was a project-existence oracle. A
    real project the delegate isn't a member of, and a project that doesn't
    exist, must yield identical responses."""
    register_project(_PROJ)
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    real = await client.get(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    bogus = await client.get(
        "/agent-mcp/api/router/projects/no-such-slug-xyz/memberships",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert real.status == bogus.status == 404
    real_body = await real.json()
    bogus_body = await bogus.json()
    assert real_body["success"] is bogus_body["success"] is False
    assert real_body["error"] == bogus_body["error"] == "not_found"


# ── Legitimate access still works ──────────────────────────────────


async def test_member_delegate_can_list_roster(
    aiohttp_client, router_app, register_project,
) -> None:
    """A delegate WITH a resolved role on the project still gets the roster —
    the scoping guard must not over-reject a legitimate member."""
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="viewer")

    resp = await client.get(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert any(r.get("user_id") == alice_id for r in body["memberships"])


async def test_sysadmin_can_list_roster(
    aiohttp_client, router_app, register_project,
) -> None:
    """A sysadmin may read any roster — the scoping guard must not shadow the
    legitimate sysadmin path."""
    register_project(_PROJ)
    _seed_user("root", is_sysadmin=True)
    member_id = _seed_user("carol", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=member_id, role="operator")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.get(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert any(r.get("user_id") == member_id for r in body["memberships"])
