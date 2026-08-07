"""DiD-R7 — client-config / installer require operator-MEMBERSHIP.

The two wiring routes embed a LIVE agent bearer for the target project.
Round-6 (#322) gated them on the delegable ``system.projects.manage``
cap — the same cap as create / delete / rename. But that cap is
DEPLOYMENT-WIDE: a sysadmin can delegate it (Wave-9 group model) to a
group whose members are NOT members of the target project, so a
delegated-cap-only NON-MEMBER could pull another tenant's live agent
bearer.

Currently INERT (``_resolve_agent_token`` returns an empty map because
``GET /api/tokens`` needs confirmed-operator tier), but a latent
landmine. DiD fix: layer an operator-membership check on top of the cap
gate for these two routes only — sysadmin OR project-``operator``
admits; a delegated-cap-only non-member and a viewer are denied.

The delegated-cap-non-member denial is RED against origin/main (#322),
where the bare cap admits.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}
_CAP = "system.projects.manage"
_WIRING = ["/client-config", "/installer"]


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username, password="passwordpassword", *, is_sysadmin=False):
    identity = _identity_module()
    with identity._connect() as conn:
        cur = conn.execute("SELECT COUNT(*) AS n FROM users")
        is_empty = cur.fetchone()["n"] == 0
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


def _create_group(group_id, name):
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, 0, '2026-06-30T00:00:00')",
            (group_id, name),
        )
    return group_id


def _grant_capability(group_id, *caps):
    from agent_mcp.repositories import group_capability_repository

    group_capability_repository.replace(group_id, caps)


def _add_membership(user_id, project_name, *, role="operator"):
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project_name, user_id, role),
        )


async def _login(client, username, password="passwordpassword"):
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    name_val = set_cookie.split(";", 1)[0]
    _, _, value = name_val.partition("=")
    return value.strip()


async def _get(client, suffix, cookie):
    return await client.get(
        f"/agent-mcp/api/router/projects/victim{suffix}",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


# ── The DiD tightening: delegated-cap NON-MEMBER is denied ──────────


@pytest.mark.parametrize("suffix", _WIRING)
async def test_delegated_cap_non_member_denied_wiring(
    aiohttp_client, router_app, router_module, register_project, suffix,
) -> None:
    """A caller holding ``system.projects.manage`` via a group but who is
    NOT a member of the target project must be DENIED the wiring routes.

    RED against #322: the bare cap admitted a non-member there.
    """
    from agent_mcp.router import group_resolver

    register_project("victim")
    router_module._agent_token_cache["victim"] = (
        9.9e18, {"tok-secret": "Admin"},
    )
    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _create_group("g-proj-admins", "Project Admins")
    _grant_capability(group_id, _CAP)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await _get(client, suffix, cookie)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"
    # The live bearer never appears in the denied body.
    assert "tok-secret" not in (await resp.text())


# ── Operator MEMBER admits (the intended trust level) ───────────────


@pytest.mark.parametrize("suffix", _WIRING)
async def test_operator_member_admits_wiring(
    aiohttp_client, router_app, router_module, register_project, suffix,
) -> None:
    """A project ``operator`` member holding the cap admits — the tighter
    gate must not lock out legitimate operators.
    """
    from agent_mcp.router import group_resolver

    register_project("victim")
    bob_id = _seed_user("bob", is_sysadmin=False)
    # Cap via a group (delegated) AND direct operator membership.
    group_id = _create_group("g-ops", "Ops")
    _grant_capability(group_id, _CAP)
    group_resolver.add_group_member(group_id, member_user_id=bob_id)
    _add_membership(bob_id, "victim", role="operator")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "bob")
    resp = await _get(client, suffix, cookie)

    # The membership gate must ADMIT an operator member. The handler
    # itself may still 404 ("unknown agent") because the token map is
    # inert in the test env — the point is the gate does not 403.
    assert resp.status != 403, await resp.text()


# ── Viewer member is denied (membership alone isn't enough) ─────────


@pytest.mark.parametrize("suffix", _WIRING)
async def test_viewer_member_denied_wiring(
    aiohttp_client, router_app, router_module, register_project, suffix,
) -> None:
    """A VIEWER-tier member with the cap is still denied — only operator
    membership (or sysadmin) may fetch the live bearer.
    """
    from agent_mcp.router import group_resolver

    register_project("victim")
    carol_id = _seed_user("carol", is_sysadmin=False)
    group_id = _create_group("g-viewers", "Viewers")
    _grant_capability(group_id, _CAP)
    group_resolver.add_group_member(group_id, member_user_id=carol_id)
    _add_membership(carol_id, "victim", role="viewer")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "carol")
    resp = await _get(client, suffix, cookie)

    assert resp.status == 403, await resp.text()


# ── Sysadmin still admits (regression guard) ────────────────────────


@pytest.mark.parametrize("suffix", _WIRING)
async def test_sysadmin_admits_wiring(
    aiohttp_client, router_app, router_module, register_project, suffix,
) -> None:
    register_project("victim")
    _seed_user("root", is_sysadmin=True)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await _get(client, suffix, cookie)

    # The membership gate must NOT be what stops a sysadmin.
    assert resp.status != 403, await resp.text()
