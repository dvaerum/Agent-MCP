"""Security round 6 (pentest R6-F2): scope the project LIFECYCLE mutations
(stop / rename / delete) to project members + close the existence oracle —
complete the R4-F3 per-project-route class-sweep.

The three lifecycle MUTATIONS live in ``admin_api.py``:

  * ``POST   /api/router/projects/<name>/stop``   (``stop_project_handler``)
  * ``PATCH  /api/router/projects/<name>``        (``rename_project_handler``)
  * ``DELETE /api/router/projects/<name>``        (``delete_project_handler``)

They were gated ONLY by the coarse deployment-wide ``system.projects.manage``
cap — a DELEGABLE table-management authority (Wave-9 group model) — with NO
per-project membership scope. Their sibling routes were already tightened:
``client-config`` / ``installer`` carry an operator-membership gate (DiD-R7);
the alias READ + DELETE were scoped to members returning ``404 unknown_project``
for a non-member (R4-F3). But stop / rename / delete stayed on the bare cap, so:

  * a non-sysadmin delegate holding ``system.projects.manage`` via a group,
    with ZERO membership in project P (P is hidden from its own ``/projects``
    and ``/overview`` views), could nonetheless STOP (cross-tenant DoS),
    RENAME, or DELETE (cross-tenant destroy) P; and
  * the 200 (real, non-member) vs 404 (nonexistent) differential was a
    deployment-wide project-existence oracle — the exact oracle R4-F3 closed
    on the read routes.

Fix (mirrors the alias routes — the R4-F3 fix): after admitting sysadmins, the
three mutations share ``_deny_cross_tenant_project_read``, so a non-member gets
the SAME 404 ``unknown_project`` for a real-but-hidden project as for a
nonexistent slug — the two cases are indistinguishable, and the mutation never
runs.

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


# ── Helpers (mirror test_sec_r4_alias_usage_scoping.py) ─────────────


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


# ── Per-mutation request helpers ───────────────────────────────────


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


# ── R6-F2: cross-tenant lifecycle mutation + existence oracle ──────
#
# For each mutation: a delegate with the cap but NO membership must get the
# SAME 404 ``not_found`` for a real-but-hidden project as for a nonexistent
# slug (existence oracle closed), and the mutation must be a no-op.


async def test_delegate_without_membership_cannot_stop(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    real = await _stop(client, cookie, _PROJ)
    bogus = await _stop(client, cookie, "no-such-slug-xyz")

    assert real.status == bogus.status == 404, await real.text()
    real_body = await real.json()
    bogus_body = await bogus.json()
    assert real_body["success"] is bogus_body["success"] is False
    assert real_body["error"] == bogus_body["error"] == "not_found"
    # The project name must not be reflected differently between the two.
    assert real_body == bogus_body or (
        real_body["error"] == bogus_body["error"]
    )
    # No 200 "stopped" leaked.
    assert "stopped" not in real_body


async def test_delegate_without_membership_cannot_rename(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    real = await _rename(client, cookie, _PROJ)
    bogus = await _rename(client, cookie, "no-such-slug-xyz")

    assert real.status == bogus.status == 404, await real.text()
    real_body = await real.json()
    bogus_body = await bogus.json()
    assert real_body["success"] is bogus_body["success"] is False
    assert real_body["error"] == bogus_body["error"] == "not_found"
    assert "renamed" not in real_body
    # The rename was a no-op — the project keeps its original name.
    assert router_module._REGISTRY.get(_PROJ) is not None
    assert router_module._REGISTRY.get("renamed-x") is None


async def test_delegate_without_membership_cannot_delete(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    real = await _delete(client, cookie, _PROJ)
    bogus = await _delete(client, cookie, "no-such-slug-xyz")

    assert real.status == bogus.status == 404, await real.text()
    real_body = await real.json()
    bogus_body = await bogus.json()
    assert real_body["success"] is bogus_body["success"] is False
    assert real_body["error"] == bogus_body["error"] == "not_found"
    assert "unregistered" not in real_body
    # The delete was a no-op — the project still exists.
    assert router_module._REGISTRY.get(_PROJ) is not None


# ── Legitimate access still works — sysadmin bypass intact ──────────


async def test_sysadmin_can_stop(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await _stop(client, cookie, _PROJ)

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["stopped"] == _PROJ


async def test_sysadmin_can_rename(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await _rename(client, cookie, _PROJ, new_name="renamed-ok")

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["renamed"]["from"] == _PROJ
    assert body["renamed"]["to"] == "renamed-ok"


async def test_sysadmin_can_delete(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await _delete(client, cookie, _PROJ)

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["unregistered"] == _PROJ
    assert router_module._REGISTRY.get(_PROJ) is None


# ── Legitimate access still works — operator-member not over-rejected


async def test_operator_member_delegate_can_stop(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="operator")

    resp = await _stop(client, cookie, _PROJ)

    assert resp.status == 200, await resp.text()
    assert (await resp.json())["stopped"] == _PROJ


async def test_operator_member_delegate_can_rename(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="operator")

    resp = await _rename(client, cookie, _PROJ, new_name="renamed-member")

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["renamed"]["to"] == "renamed-member"


async def test_operator_member_delegate_can_delete(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="operator")

    resp = await _delete(client, cookie, _PROJ)

    assert resp.status == 200, await resp.text()
    assert (await resp.json())["unregistered"] == _PROJ
    assert router_module._REGISTRY.get(_PROJ) is None
