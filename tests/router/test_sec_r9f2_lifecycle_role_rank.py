"""Security round 9 (pentest R9-F2, HIGHER severity than R8-F3 — NO RACE
NEEDED): ``_deny_cross_tenant_project_read`` (``admin_api.py``) only checks
project-membership EXISTENCE, never role TIER.

A non-sysadmin holding the deployment-wide (delegable) ``system.projects.
manage`` capability via a group grant, PLUS a mere ``viewer``-tier project
membership, could fully rename / stop / delete that project with a SINGLE
ordinary (non-racing) request — no TOCTOU window required, unlike R8-F3.

This is pre-existing since R6-F2 (#322): that fix scoped the three lifecycle
mutations (+ the alias routes) to project MEMBERS, closing the cross-tenant
existence oracle, but never compared the member's role RANK against the
destructive action being performed. It is inconsistent with the codebase's
own adjacent invariant — ``_membership_grant_denied``
(``admin_users_api.py``) already requires the caller's LIVE role rank to be
at or above the role being granted/revoked for membership-CRUD writes.
rename / stop / delete / remove-alias had no equivalent rank guard at all.

Live repro (round-9 pentest lane, single ordinary request per step, no
race):
  1. sysadmin creates project X, group G granted ``system.projects.manage``,
     user alice in G with an ``operator`` project-membership.
  2. sysadmin demotes alice to ``viewer`` on project X (normal PATCH
     membership call).
  3. alice (now viewer) sends ONE ordinary PATCH rename -> pre-fix 200.
  4. alice sends ONE ordinary POST stop -> pre-fix 200.
  5. alice sends ONE ordinary DELETE -> pre-fix 200.
  6. alice sends ONE ordinary DELETE .../aliases/<alias> -> pre-fix 200.

Fix: ``_deny_cross_tenant_project_read`` gained an optional ``min_role``
keyword. When set, a resolved member whose ``store.role_rank(role)`` is
below ``store.role_rank(min_role)`` gets a 403 ``forbidden`` (NOT the 404
non-member oracle-closer — they ARE a legitimate, disclosed member; only
their AUTHORITY for this destructive action is insufficient). rename /
delete / stop / remove-alias now pass ``min_role="operator"``; the
read-only ``alias_usage_handler`` is untouched (default ``min_role=None``
== "any resolved membership", its pre-existing behaviour). Sysadmins keep
bypassing unconditionally.
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

_PROJ = "proj-r9f2"


# ── Helpers (mirror test_sec_r6_lifecycle_scoping.py) ───────────────


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
    """Log in a non-sysadmin operator 'alice' who carries ``caps`` via a
    group capability grant. Returns (client, cookie, alice_id, group_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated-r9f2", "Delegated Admins R9F2")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id, group_id


# ── Per-mutation request helpers ────────────────────────────────────


async def _stop(client, cookie, project: str):
    return await client.post(
        f"/agent-mcp/api/router/projects/{project}/stop",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


async def _rename(client, cookie, project: str, new_name: str = "renamed-x"):
    return await client.patch(
        f"/agent-mcp/api/router/projects/{project}",
        data=json.dumps({"name": new_name, "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


async def _delete(client, cookie, project: str):
    return await client.delete(
        f"/agent-mcp/api/router/projects/{project}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


async def _remove_alias(client, cookie, project: str, alias: str):
    return await client.delete(
        f"/agent-mcp/api/router/projects/{project}/aliases/{alias}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


# ── R9-F2: viewer-tier member + coarse cap must NOT reach destructive
# lifecycle ops — single ordinary (non-racing) request each. ───────


async def test_viewer_member_delegate_cannot_stop(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="viewer")

    resp = await _stop(client, cookie, _PROJ)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    # No 200 "stopped" leaked, and the backend must not actually be
    # stopped as a side effect of a rejected request.
    assert "stopped" not in body


async def test_viewer_member_delegate_cannot_rename(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="viewer")

    resp = await _rename(client, cookie, _PROJ)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    assert "renamed" not in body
    # No-op: the project keeps its original name.
    assert router_module._REGISTRY.get(_PROJ) is not None
    assert router_module._REGISTRY.get("renamed-x") is None


async def test_viewer_member_delegate_cannot_delete(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="viewer")

    resp = await _delete(client, cookie, _PROJ)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    assert "unregistered" not in body
    # No-op: the project still exists.
    assert router_module._REGISTRY.get(_PROJ) is not None


async def test_viewer_member_delegate_cannot_remove_alias(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="viewer")
    # Give the project an alias to try to remove (created via a real
    # sysadmin rename so the grace-alias row exists).
    root_client = await aiohttp_client(router_app)
    _seed_user("root-alias-setup", is_sysadmin=True)
    root_cookie = await _login(root_client, "root-alias-setup")
    rename_resp = await root_client.patch(
        f"/agent-mcp/api/router/projects/{_PROJ}",
        data=json.dumps({"name": f"{_PROJ}-new", "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": root_cookie},
        allow_redirects=False,
    )
    assert rename_resp.status == 200, await rename_resp.text()
    # alice's membership row is keyed by project_name and the rename
    # migrates it to the new name automatically — no re-seed needed; a
    # second insert here would UNIQUE-violate on (project_name, user_id).

    resp = await _remove_alias(client, cookie, f"{_PROJ}-new", _PROJ)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    assert "removed" not in body


# ── Regression: sysadmin bypass is unconditional and unaffected ────


async def test_sysadmin_can_stop_rename_delete_regardless_of_membership(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """A sysadmin must still be able to stop / rename / delete a project
    they hold NO project-membership row on at all — the existing
    sysadmin-bypass behaviour in ``_deny_cross_tenant_project_read`` must
    not regress when the rank check is added."""
    stop_proj = "r9f2-sysadmin-stop"
    rename_proj = "r9f2-sysadmin-rename"
    delete_proj = "r9f2-sysadmin-delete"
    register_project(stop_proj)
    register_project(rename_proj)
    register_project(delete_proj)
    _seed_user("root-r9f2", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root-r9f2")

    stop_resp = await _stop(client, cookie, stop_proj)
    assert stop_resp.status == 200, await stop_resp.text()
    assert (await stop_resp.json())["stopped"] == stop_proj

    rename_resp = await _rename(
        client, cookie, rename_proj, new_name="r9f2-sysadmin-renamed",
    )
    assert rename_resp.status == 200, await rename_resp.text()
    assert (await rename_resp.json())["renamed"]["to"] == (
        "r9f2-sysadmin-renamed"
    )

    delete_resp = await _delete(client, cookie, delete_proj)
    assert delete_resp.status == 200, await delete_resp.text()
    assert (await delete_resp.json())["unregistered"] == delete_proj
    assert router_module._REGISTRY.get(delete_proj) is None


# ── Regression: an operator-tier member (not just sysadmin) must still
# be able to perform all four actions — the rank check must not
# over-reject a legitimately-authorised member. ─────────────────────


async def test_operator_member_delegate_can_stop_rename_delete_remove_alias(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    stop_proj = "r9f2-operator-stop"
    rename_proj = "r9f2-operator-rename"
    delete_proj = "r9f2-operator-delete"
    alias_proj = "r9f2-operator-alias"
    for p in (stop_proj, rename_proj, delete_proj, alias_proj):
        register_project(p)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    for p in (stop_proj, rename_proj, delete_proj, alias_proj):
        _seed_project_membership(p, user_id=alice_id, role="operator")

    stop_resp = await _stop(client, cookie, stop_proj)
    assert stop_resp.status == 200, await stop_resp.text()

    rename_resp = await _rename(
        client, cookie, rename_proj, new_name="r9f2-operator-renamed",
    )
    assert rename_resp.status == 200, await rename_resp.text()

    delete_resp = await _delete(client, cookie, delete_proj)
    assert delete_resp.status == 200, await delete_resp.text()
    assert router_module._REGISTRY.get(delete_proj) is None

    # Create a real alias on alias_proj (rename it, keeping the old name
    # as an alias), then confirm alice (operator) can remove that alias.
    rename_for_alias = await _rename(
        client, cookie, alias_proj, new_name=f"{alias_proj}-new",
    )
    assert rename_for_alias.status == 200, await rename_for_alias.text()
    # alice's membership migrates with the rename — no re-seed needed.
    remove_resp = await _remove_alias(
        client, cookie, f"{alias_proj}-new", alias_proj,
    )
    assert remove_resp.status == 200, await remove_resp.text()
    body = await remove_resp.json()
    assert body["removed"] == alias_proj


# ── Regression: alias_usage_handler (read-only) is UNAFFECTED — any
# resolved membership (even viewer) still suffices to read alias usage.
# ─────────────────────────────────────────────────────────────────────


async def test_viewer_member_delegate_can_still_read_alias_usage(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    root_client = await aiohttp_client(router_app)
    _seed_user("root-r9f2-alias-read", is_sysadmin=True)
    root_cookie = await _login(root_client, "root-r9f2-alias-read")
    proj = "r9f2-viewer-alias-read"
    register_project(proj)
    rename_resp = await root_client.patch(
        f"/agent-mcp/api/router/projects/{proj}",
        data=json.dumps({"name": f"{proj}-new", "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": root_cookie},
        allow_redirects=False,
    )
    assert rename_resp.status == 200, await rename_resp.text()

    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(
        f"{proj}-new", user_id=alice_id, role="viewer",
    )

    resp = await client.get(
        f"/agent-mcp/api/router/projects/{proj}-new/aliases?alias={proj}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["alias"] == proj
