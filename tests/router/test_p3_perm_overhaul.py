"""Phase 3 Wave 2 (v5.0.69) — permission overhaul tests.

The plan (``prancy-napping-pie.md`` Phase 3 Wave 2) wires the Wave-1a
resolvers (``resolve_user_is_sysadmin``,
``resolve_user_project_role``) and the Wave-1b CRUD surface into the
route gates. Before this wave:

  * Wave 1b user/group/membership routes accepted any logged-in
    operator. There was no per-route gate.
  * The aiohttp middleware
    (``require_operator_session_middleware``) checked project
    membership existence but ignored ``role`` — viewers could mutate
    just like operators.
  * Project create / delete (``/api/router/projects``) accepted any
    logged-in operator. Non-sysadmin operators could create projects.

This module pins the Phase 3 model:

  * **Sysadmin-only gates** — project create/delete, user CRUD,
    group CRUD all 403 a non-sysadmin caller.
  * **Per-project operator/viewer gates** — viewers can READ
    ``/api/<project>/...`` paths but cannot MUTATE them. Operators
    can do both. Sysadmin can do both regardless of membership.
  * **Group inheritance** — a user in a nested group whose parent
    has ``is_sysadmin=1`` passes the sysadmin check via
    ``resolve_user_is_sysadmin``'s transitive closure.
  * **Caching** — repeat resolution calls inside one request session
    don't blow up; the resolver remains correct across consecutive
    requests in the same TestClient session.

Tests intentionally bypass the dashboard JS and exercise the HTTP
contract directly so the seam being asserted is the auth seam, not
any frontend behaviour.
"""

from __future__ import annotations

import json

import pytest


# Bypass the conftest auto-login: each test seeds its own user(s)
# and logs in explicitly so the assertion targets the real
# sysadmin/operator/viewer distinction rather than a side-effect of
# the sentinel-operator fixture (which is created without any group
# membership and is NOT a sysadmin by default).
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers ─────────────────────────────────────────────────────────


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
    """Create a user via ``identity.create_user`` and optionally
    promote them to sysadmin in the same step."""
    identity = _identity_module()
    user_id = identity.create_user(username=username, password=password)
    if is_sysadmin:
        with identity._connect() as conn:
            conn.execute(
                "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
                (user_id,),
            )
    return user_id


def _add_membership(
    user_id: str, project_name: str, *, role: str = "operator",
) -> None:
    """Grant ``user_id`` membership in ``project_name`` at the given
    tier. Uses the identity._connect() handle because
    ``identity.add_project_membership`` predates the ``role`` column
    and always defaults to 'operator'."""
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project_name, user_id, role),
        )


async def _login(client, username: str, password: str = "passwordpassword") -> str:
    """POST /agent-mcp/login and return the session cookie value."""
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


# ── Sysadmin gates: project create / delete ────────────────────────


async def test_non_sysadmin_cannot_create_project(
    aiohttp_client, router_app,
) -> None:
    """A logged-in operator who is NOT a sysadmin must NOT be able
    to POST ``/api/router/projects``. The Phase-3 system perm matrix
    reserves project create/delete for sysadmins.
    """
    _seed_user("alice", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "proj-z"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()


async def test_sysadmin_can_create_project(
    aiohttp_client, router_app,
) -> None:
    """A sysadmin can POST ``/api/router/projects`` and receives the
    expected 201 / validation envelope (never 403)."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "proj-ok"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status != 403, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


async def test_non_sysadmin_cannot_delete_project(
    aiohttp_client, router_app, register_project,
) -> None:
    """Project deletion is a sysadmin-only action. A non-sysadmin
    operator who is even a project member must still be 403'd."""
    register_project("delete-me")
    user_id = _seed_user("alice", is_sysadmin=False)
    _add_membership(user_id, "delete-me", role="operator")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.delete(
        "/agent-mcp/api/router/projects/delete-me",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()


# ── Sysadmin gates: user CRUD ──────────────────────────────────────


async def test_non_sysadmin_cannot_create_user(
    aiohttp_client, router_app,
) -> None:
    """The Wave 1b user-CRUD surface ships behind a "any logged-in
    operator" gate. Wave 2 tightens it to sysadmin-only."""
    _seed_user("alice", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "newcomer",
            "password": "newcomerpassword",
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()


async def test_sysadmin_can_create_user(
    aiohttp_client, router_app,
) -> None:
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "newcomer",
            "password": "newcomerpassword",
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status != 403, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


async def test_non_sysadmin_cannot_delete_user(
    aiohttp_client, router_app,
) -> None:
    target_id = _seed_user("victim", is_sysadmin=False)
    _seed_user("alice", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.delete(
        f"/agent-mcp/api/router/users/{target_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()


# ── Sysadmin gates: group CRUD ─────────────────────────────────────


async def test_non_sysadmin_cannot_create_group(
    aiohttp_client, router_app,
) -> None:
    _seed_user("alice", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "engineers"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()


async def test_sysadmin_can_create_group(
    aiohttp_client, router_app,
) -> None:
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "engineers"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status != 403, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


# ── Per-project operator/viewer gates ──────────────────────────────


async def test_viewer_can_read_project(
    aiohttp_client, router_app, register_project,
) -> None:
    """A user with ``role='viewer'`` on a project may issue READ
    requests against ``/agent-mcp/api/<project>/...`` — middleware
    must not 401 or 403 them. Only 5xx (backend unreachable) or
    other non-auth outcomes are acceptable."""
    register_project("alpha")
    viewer_id = _seed_user("vera", is_sysadmin=False)
    _add_membership(viewer_id, "alpha", role="viewer")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "vera")
    resp = await client.get(
        "/agent-mcp/api/alpha/tokens",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    # GET = read — viewer must pass the auth gate. The backend isn't
    # actually running in unit tests so 5xx is expected; we ONLY
    # assert that the middleware didn't 401/403 us.
    assert resp.status not in (401, 403), await resp.text()


async def test_viewer_cannot_mutate_project(
    aiohttp_client, router_app, register_project,
) -> None:
    """A viewer is read-only — POST / PATCH / DELETE on
    ``/api/<project>/...`` must 403."""
    register_project("alpha")
    viewer_id = _seed_user("vera", is_sysadmin=False)
    _add_membership(viewer_id, "alpha", role="viewer")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "vera")
    resp = await client.post(
        "/agent-mcp/api/alpha/create-agent",
        data=json.dumps({"agent_id": "agent-x"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()


async def test_operator_can_mutate_project(
    aiohttp_client, router_app, register_project,
) -> None:
    """An operator-tier member can mutate — the auth gate accepts
    the call. The backend's own response (likely 5xx in tests) is
    out of scope; what we pin is the middleware decision."""
    register_project("alpha")
    op_id = _seed_user("olive", is_sysadmin=False)
    _add_membership(op_id, "alpha", role="operator")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "olive")
    resp = await client.post(
        "/agent-mcp/api/alpha/create-agent",
        data=json.dumps({"agent_id": "agent-x"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status not in (401, 403), await resp.text()


async def test_sysadmin_can_mutate_any_project_without_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """A sysadmin doesn't need a ``project_membership`` row to
    mutate a project — the global sysadmin bit admits."""
    register_project("alpha")
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.post(
        "/agent-mcp/api/alpha/create-agent",
        data=json.dumps({"agent_id": "agent-x"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status not in (401, 403), await resp.text()


# ── Group inheritance for sysadmin ─────────────────────────────────


async def test_user_in_nested_sysadmin_group_passes(
    aiohttp_client, router_app,
) -> None:
    """The sysadmin bit transits through nested groups. Topology::

        root_group (is_sysadmin=1)
          └─ ops_group
               └─ alice

    ``resolve_user_is_sysadmin(alice)`` must return True, and the
    sysadmin-gated routes must admit alice.
    """
    identity = _identity_module()
    alice_id = _seed_user("alice", is_sysadmin=False)
    # Build the nested group structure.
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES ('g_root', 'root', 1, '2026-06-18T00:00:00')"
        )
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES ('g_ops', 'ops', 0, '2026-06-18T00:00:00')"
        )
    from agent_mcp.router import group_resolver
    group_resolver.add_group_member("g_ops", member_user_id=alice_id)
    group_resolver.add_group_member("g_root", member_group_id="g_ops")
    # Sanity-check the resolver agrees before going through HTTP.
    assert group_resolver.resolve_user_is_sysadmin(alice_id) is True

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "engineers"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    # Inherited sysadmin via nested group must admit the call.
    assert resp.status != 403, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


# ── Resolution stability across consecutive requests ──────────────


async def test_resolution_stable_across_consecutive_requests(
    aiohttp_client, router_app,
) -> None:
    """Two back-to-back sysadmin-gated requests on the same session
    both succeed — no mid-flight cache poisoning or stale resolver
    state. Belt-and-braces against the kind of "first request opens
    a cache that the second one reads stale" bug that's easy to
    introduce when adding per-request caching."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    for i in range(3):
        resp = await client.post(
            "/agent-mcp/api/router/groups",
            data=json.dumps({"name": f"team-{i}"}),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": cookie},
            allow_redirects=False,
        )
        assert resp.status != 403, (
            f"iteration {i}: unexpected 403; body={await resp.text()}"
        )
        assert resp.status in (201, 409), await resp.text()
