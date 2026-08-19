"""Pentest R1-F1: close the cross-tenant project-EXISTENCE oracle on the
CREATE-name and RENAME-new-name collision checks — the class-sweep sibling
of R4-F3 / R6-F2.

``_validate_name(name, existing)`` (``app.py``) rejects a candidate name
with a distinguishable 409 whenever it collides with ANY row in the
GLOBAL project registry (``_app._projects_dict()``), with no filter for
whether the calling operator can actually see that project. R4-F3 / R6-F2
already scoped every other per-project route (delete / rename-OLD-name /
stop / client-config / installer / alias-read) to
``_deny_cross_tenant_project_read`` so a non-member sees the SAME 404
``unknown_project`` for a real-but-hidden project as for a nonexistent
slug — but the CREATE-name and RENAME-NEW-name collision checks (both the
direct-name-taken branch AND the active-alias-collision branch) were
never swept in, so a delegate holding only the deployment-wide
``system.projects.manage`` capability, with ZERO membership on the
colliding project, could POST/PATCH a candidate name and read a
deterministic, zero-cost, name-echoing 409 vs 201/404 differential to
enumerate hidden tenant project slugs:

    POST /api/router/projects {"name": "pentest-tenant-b"} -> 409 already_registered
    POST /api/router/projects {"name": "<nonexistent>"}    -> 201 created
    PATCH /api/router/projects/<own> {"name": "pentest-tenant-b"} -> 409 name_taken

Fix: thread the SAME ``_deny_cross_tenant_project_read`` escape hatch
through both collision checks (name-taken + alias-collision) on BOTH
create and rename-new-name (including the inside-lock TOCTOU re-check),
mirroring how ``list_projects_handler`` / ``overview_handler`` /
the R6-F2 lifecycle mutations were fixed. A sysadmin, or a caller who
actually holds a resolved role on the colliding project, still gets the
real 409 (happy path unchanged); everyone else gets the uniform 404
``unknown_project`` instead — a genuinely free name and a hidden-but-taken
one both read as "unknown" to them, and no create/rename mutation happens
in the hidden-collision case.

These tests drive the real middleware + route stack (mirrors
``test_sec_r6_lifecycle_scoping.py``), so the seam asserted is the wired-up
auth/handler seam, not an in-process helper.
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

_HIDDEN = "pentest-tenant-b"
_OWN = "own-visible-project"
_ALIAS_OF_HIDDEN = "hidden-oldname"


# ── Helpers (mirror test_sec_r6_lifecycle_scoping.py) ────────────────


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


# ── Request helpers ───────────────────────────────────────────────


async def _create(client, cookie, name: str):
    return await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": name}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


async def _rename(client, cookie, project: str, new_name: str):
    return await client.patch(
        f"/agent-mcp/api/router/projects/{project}",
        data=json.dumps({"name": new_name, "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


# ── R1-F1: CREATE — delegate without membership on the colliding project ──


async def test_create_delegate_without_membership_gets_uniform_404_not_conflict(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_HIDDEN)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await _create(client, cookie, _HIDDEN)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    # The rich, name-echoing collision signal must not leak through.
    assert "already" not in json.dumps(body).lower()
    assert "registered" not in json.dumps(body).lower()
    # No membership was silently granted, and the hidden project's
    # workspace/registration is untouched.
    from agent_mcp.router import group_resolver

    assert group_resolver.resolve_user_project_role(alice_id, _HIDDEN) is None


async def test_create_delegate_without_membership_alias_collision_gets_uniform_404(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_HIDDEN)
    router_module._REGISTRY.add_alias(_HIDDEN, _ALIAS_OF_HIDDEN)
    assert (
        router_module._REGISTRY.resolve_alias(_ALIAS_OF_HIDDEN) == _HIDDEN
    )
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await _create(client, cookie, _ALIAS_OF_HIDDEN)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    assert "alias" not in json.dumps(body).lower()
    # The alias must not have been shadowed by a new real project.
    assert router_module._REGISTRY.get(_ALIAS_OF_HIDDEN) is None
    assert router_module._REGISTRY.resolve_alias(_ALIAS_OF_HIDDEN) == _HIDDEN


async def test_create_delegate_can_still_create_genuinely_free_name(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """Happy path: a delegate with the cap can still create a project whose
    name doesn't collide with anything — the fix must not break real
    creates for a legitimately-free name."""
    register_project(_HIDDEN)
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await _create(client, cookie, "brand-new-free-slug")

    assert resp.status == 201, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["project"]["name"] == "brand-new-free-slug"
    assert router_module._REGISTRY.get("brand-new-free-slug") is not None


# ── R1-F1: CREATE — happy path unaffected (sysadmin / visible member) ─────


async def test_create_sysadmin_still_gets_real_conflict_signal(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project(_HIDDEN)
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await _create(client, cookie, _HIDDEN)

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["error"] == "already_registered"


async def test_create_member_delegate_still_gets_real_conflict_signal(
    aiohttp_client, router_app, register_project,
) -> None:
    """A delegate who IS a member of the colliding project (they can see
    it in their own /projects view) must still get the informative 409 —
    only invisible collisions are folded into the uniform 404."""
    register_project(_OWN)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_OWN, user_id=alice_id, role="operator")

    resp = await _create(client, cookie, _OWN)

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["error"] == "already_registered"


# ── R1-F1: RENAME (new-name) — delegate without membership ────────────────


async def test_rename_delegate_without_membership_on_new_name_gets_uniform_404(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_HIDDEN)
    register_project(_OWN)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_OWN, user_id=alice_id, role="operator")

    resp = await _rename(client, cookie, _OWN, _HIDDEN)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    assert "taken" not in json.dumps(body).lower()
    # No-op: the caller's own project keeps its original name, and no
    # destructive step (systemctl stop / workspace move) ran.
    assert router_module._REGISTRY.get(_OWN) is not None
    assert router_module._REGISTRY.get(_HIDDEN) is not None


async def test_rename_delegate_without_membership_alias_collision_gets_uniform_404(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_HIDDEN)
    register_project(_OWN)
    router_module._REGISTRY.add_alias(_HIDDEN, _ALIAS_OF_HIDDEN)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_OWN, user_id=alice_id, role="operator")

    resp = await _rename(client, cookie, _OWN, _ALIAS_OF_HIDDEN)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    assert router_module._REGISTRY.get(_OWN) is not None


# ── R1-F1: RENAME — happy path unaffected (sysadmin / visible member) ─────


async def test_rename_sysadmin_still_gets_real_conflict_signal(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project(_HIDDEN)
    register_project(_OWN)
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await _rename(client, cookie, _OWN, _HIDDEN)

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["error"] == "name_taken"


async def test_rename_member_delegate_still_gets_real_conflict_signal(
    aiohttp_client, router_app, register_project,
) -> None:
    """The delegate is a member of BOTH the source and the colliding
    target project — the real, informative 409 must be unaffected."""
    register_project(_OWN)
    other_visible = "other-visible-project"
    register_project(other_visible)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_OWN, user_id=alice_id, role="operator")
    _seed_project_membership(other_visible, user_id=alice_id, role="operator")

    resp = await _rename(client, cookie, _OWN, other_visible)

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["error"] == "name_taken"
