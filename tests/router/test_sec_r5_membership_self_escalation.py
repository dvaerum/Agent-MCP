"""Security round 5: project-membership self-escalation (AZ-R5-1 [HIGH]).

The unguarded sibling of the round-4 AZ-1/AZ-2 amplification fix. The
project-membership write routes —

    POST  /api/router/projects/<name>/memberships          (add)
    PATCH /api/router/projects/<name>/memberships/<mid>     (change role)

— are gated ONLY by ``system.projects.manage``, a DELEGABLE
table-management cap. But the per-project DATA middleware
(``auth_middleware``) gates ``/api/<project>/…`` on ``project_membership``,
NOT on that cap. So a non-sysadmin delegate self-writing a membership row
converts table-management authority into cross-tenant DATA authority:

  * add THEMSELVES as ``operator`` to a project they have no relationship
    with, or
  * add a GROUP they belong to as ``operator`` (group-indirection bypasses
    any user-row-only check), or
  * PATCH their own ``viewer`` row up to ``operator``.

Fix (mirrors the round-4 ``_caps_caller_lacks`` guard): a non-sysadmin
may only confer membership at or below their OWN effective role on that
project, and only if they hold a membership there at all. Sysadmins keep
full behaviour.

These tests drive the real middleware + route stack, so the seam asserted
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

_PROJ = "proj-x"


# ── Helpers (mirror test_sec_r4_cap_amplification.py) ──────────────


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
    *, user_id: str | None = None, group_id: str | None = None,
    role: str = "operator",
) -> None:
    """Insert a project_membership row directly (role-explicit, unlike
    ``identity.add_project_membership`` which always defaults operator)."""
    identity = _identity_module()
    with identity._connect() as conn:
        if user_id is not None:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, role) VALUES (?, ?, ?)",
                (_PROJ, user_id, role),
            )
        else:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, group_id, role) "
                "VALUES (?, NULL, ?, ?)",
                (_PROJ, group_id, role),
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
    group capability grant. Returns (client, cookie, alice_id, group_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id, group_id


# ── AZ-R5-1: add self as operator (no membership) ──────────────────


async def test_delegate_cannot_self_add_operator_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """A non-sysadmin holding only ``system.projects.manage`` with NO
    membership on the project must NOT be able to POST themselves an
    operator membership — that self-writes cross-tenant data access."""
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await client.post(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        data=json.dumps({"user_id": alice_id, "role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


# ── AZ-R5-1: group-indirection ─────────────────────────────────────


async def test_delegate_cannot_self_add_operator_via_own_group(
    aiohttp_client, router_app, register_project,
) -> None:
    """Group-indirection must be guarded too: a delegate with no membership
    may not add a GROUP THEY BELONG TO as operator (they would inherit the
    operator data access through that group). Expect 403."""
    register_project(_PROJ)
    client, cookie, _alice_id, group_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await client.post(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        data=json.dumps({"group_id": group_id, "role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


# ── AZ-R5-1: viewer→operator self-PATCH ────────────────────────────


async def test_viewer_delegate_cannot_patch_self_up_to_operator(
    aiohttp_client, router_app, register_project,
) -> None:
    """A viewer-role delegate may not PATCH their own membership up to
    operator — conferring a role above their own. Expect 403."""
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(user_id=alice_id, role="viewer")

    resp = await client.patch(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{alice_id}",
        data=json.dumps({"role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


# ── Legitimate delegation (≤ own role) still allowed ───────────────


async def test_operator_delegate_can_grant_viewer_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression guard: an operator-role delegate granting VIEWER (a role
    at or below their own) to someone else still succeeds — the guard only
    blocks escalation beyond the caller's own authority."""
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(user_id=alice_id, role="operator")
    bob_id = _seed_user("bob", is_sysadmin=False)

    resp = await client.post(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        data=json.dumps({"user_id": bob_id, "role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 201, await resp.text()


# ── Sysadmin keeps full behaviour ──────────────────────────────────


async def test_sysadmin_can_grant_operator_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """A sysadmin may confer any membership — the round-5 guard must not
    over-reject the legitimate sysadmin path."""
    register_project(_PROJ)
    _seed_user("root", is_sysadmin=True)
    target_id = _seed_user("carol", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.post(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        data=json.dumps({"user_id": target_id, "role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 201, await resp.text()
