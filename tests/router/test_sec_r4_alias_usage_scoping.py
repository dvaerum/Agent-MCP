"""Security round 4 (pentest R4-F3): scope the alias-usage READ to project
members + close the project-existence oracle — complete the R3-F1 class-sweep.

``GET /api/router/projects/<name>/aliases?alias=<a>`` (``alias_usage_handler``)
was gated ONLY by the coarse deployment-wide ``system.projects.manage`` cap —
a DELEGABLE table-management authority — with NO per-project membership
scoping. Its sibling READ ``list_project_memberships_handler`` was tightened by
R3-F1 (#468) to return ``404 unknown_project`` for a non-member; its wiring
siblings ``client-config`` / ``installer`` carry an operator-membership gate
(DiD-R7). But this handler stayed on the bare cap, so:

  * a non-sysadmin delegate holding ``system.projects.manage`` via a group,
    with ZERO membership in project P, could ``GET …/projects/P/aliases`` and,
    once an alias existed, read the **agent_ids that used it + its expiry** —
    a cross-tenant disclosure of a project hidden from their own ``/projects``
    and ``/overview`` views; and
  * the reach-the-handler (real) vs 404 (nonexistent) differential was a
    project-existence oracle.

Fix (mirrors ``list_project_memberships_handler`` — the R3-F1 fix): after
confirming the project exists, admit only a sysadmin OR a caller with a
resolved role on the project; otherwise return the SAME 404 ``unknown_project``
a non-member sees for a nonexistent slug, so the two cases are
indistinguishable.

These tests drive the real middleware + route stack, so the seam asserted is
the wired-up auth/handler seam, not an in-process helper.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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
_ALIAS = "oldname"


# ── Helpers (mirror test_sec_r3_membership_list_scoping.py) ─────────


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


def _seed_alias_with_usage(router_module, workspace: Path) -> None:
    """Register an alias on ``_PROJ`` and record agent_ids that used it so the
    handler has a real roster to (not) disclose."""
    router_module._REGISTRY.add_alias(_PROJ, _ALIAS)
    db_dir = Path(workspace) / ".agent"
    db_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_dir / "mcp_state.db")
    try:
        cur = con.cursor()
        cur.execute(
            "CREATE TABLE mcp_sessions ("
            "session_id TEXT PRIMARY KEY, agent_id TEXT, alias_used TEXT, "
            "last_seen_at TEXT)"
        )
        cur.execute(
            "INSERT INTO mcp_sessions "
            "(session_id, agent_id, alias_used, last_seen_at) "
            "VALUES ('s1', 'agent-secret', ?, '2026-06-01T10:00:00Z')",
            (_ALIAS,),
        )
        con.commit()
    finally:
        con.close()


async def _get_aliases(client, cookie, project: str, alias: str = _ALIAS):
    return await client.get(
        f"/agent-mcp/api/router/projects/{project}/aliases?alias={alias}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


# ── R4-F3: cross-tenant alias-roster disclosure (the live exploit) ─


async def test_delegate_without_membership_cannot_read_alias_usage(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """A non-sysadmin holding only ``system.projects.manage`` with NO
    membership on the project must NOT reach the alias-usage roster — it
    returns 404 ``unknown_project`` (was reaching the handler / 200 + the
    agent_ids that used the alias)."""
    workspace = register_project(_PROJ)
    _seed_alias_with_usage(router_module, workspace)

    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await _get_aliases(client, cookie, _PROJ)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    # No alias roster leaked in the body.
    assert "agents" not in body
    assert "agent-secret" not in json.dumps(body)


# ── R4-F3: existence oracle closed ─────────────────────────────────


async def test_alias_usage_real_nonmember_indistinguishable_from_nonexistent(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """The reach-the-handler / 404 differential was a project-existence
    oracle. A real project the delegate isn't a member of, and a project that
    doesn't exist, must yield identical responses."""
    workspace = register_project(_PROJ)
    _seed_alias_with_usage(router_module, workspace)
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    real = await _get_aliases(client, cookie, _PROJ)
    bogus = await _get_aliases(client, cookie, "no-such-slug-xyz")

    assert real.status == bogus.status == 404
    real_body = await real.json()
    bogus_body = await bogus.json()
    assert real_body["success"] is bogus_body["success"] is False
    assert real_body["error"] == bogus_body["error"] == "not_found"


# ── Legitimate access still works ──────────────────────────────────


async def test_member_delegate_can_read_alias_usage(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """A delegate WITH a resolved role on the project still reaches the
    handler — the scoping guard must not over-reject a legitimate member."""
    workspace = register_project(_PROJ)
    _seed_alias_with_usage(router_module, workspace)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="viewer")

    resp = await _get_aliases(client, cookie, _PROJ)

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["alias"] == _ALIAS
    assert body["project"] == _PROJ
    assert body["agents"] == ["agent-secret"]


async def test_sysadmin_can_read_alias_usage(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """A sysadmin may read any alias roster — the scoping guard must not
    shadow the legitimate sysadmin path."""
    workspace = register_project(_PROJ)
    _seed_alias_with_usage(router_module, workspace)
    _seed_user("root", is_sysadmin=True)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await _get_aliases(client, cookie, _PROJ)

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["alias"] == _ALIAS
    assert body["agents"] == ["agent-secret"]


# ── R4-F3 class-sweep: the alias DELETE shares the same coarse gate ──


async def _delete_alias(client, cookie, project: str, alias: str = _ALIAS):
    return await client.delete(
        f"/agent-mcp/api/router/projects/{project}/aliases/{alias}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


async def test_delegate_without_membership_cannot_remove_alias(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """The alias DELETE is the write-side sibling of the read finding — it
    shares the coarse ``project_lifecycle_gate``. A non-sysadmin holding only
    ``system.projects.manage`` with NO membership must NOT expire an alias on
    a project hidden from its views, and must get the SAME 404 as a
    nonexistent slug (no 200-vs-404 existence oracle, no cross-tenant write)."""
    workspace = register_project(_PROJ)
    _seed_alias_with_usage(router_module, workspace)
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    real = await _delete_alias(client, cookie, _PROJ)
    bogus = await _delete_alias(client, cookie, "no-such-slug-xyz")

    assert real.status == bogus.status == 404, await real.text()
    real_body = await real.json()
    assert real_body["success"] is False
    assert real_body["error"] == "not_found"
    # The alias must still be present — the unauthorized DELETE was a no-op.
    _sysadmin = _seed_user("root2", is_sysadmin=True)
    sclient = await aiohttp_client(router_app)
    scookie = await _login(sclient, "root2")
    still = await _get_aliases(sclient, scookie, _PROJ)
    assert still.status == 200
    assert (await still.json())["agents"] == ["agent-secret"]


async def test_member_delegate_can_remove_alias(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """A delegate WITH a resolved role on the project may still remove its
    alias — the scoping guard must not over-reject a legitimate member."""
    workspace = register_project(_PROJ)
    _seed_alias_with_usage(router_module, workspace)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="operator")

    resp = await _delete_alias(client, cookie, _PROJ)

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["removed"] == _ALIAS
    assert body["project"] == _PROJ
