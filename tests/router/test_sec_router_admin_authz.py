"""Security: router-admin wiring/lifecycle routes must be capability-gated.

Owner-authorized defensive review (2026-07-09), FINDING 1 [HIGH].

Five router-admin routes in ``router/admin_api.py`` were registered
with only ``gated(...)`` (the Accept-header + operator-session gate) and
NO capability gate — unlike the sibling create / rename / delete routes,
which carry ``require_capability("system.projects.manage")``:

  * ``GET  .../projects/<name>/client-config``  — embeds a LIVE agent bearer
  * ``GET  .../projects/<name>/installer``       — embeds a LIVE agent bearer
  * ``POST .../projects/<name>/stop``            — bounces a project backend
  * ``GET  .../projects/<name>/aliases``         — enumerates alias users
  * ``DELETE .../projects/<name>/aliases/<a>``   — expires an alias

Because the ``router`` segment is in ``_NON_PROJECT_API_SEGMENTS``
(``auth_middleware``), the project-membership middleware never checks
``<name>`` either. Net: any authenticated caller — even a *viewer* of an
UNRELATED project — could read ANOTHER project's live agent token and
connect as that agent (cross-tenant disclosure), or DoS/mutate another
project's backend and aliases.

Fix: wrap all five in ``require_capability("system.projects.manage")`` to
match the sibling lifecycle gate. A viewer / non-authorized operator is
denied (403); a sysadmin (or an operator delegated the cap via a group)
passes.
"""

from __future__ import annotations

import pytest


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}
_CAP = "system.projects.manage"


# ── Helpers (mirror test_sec_admin_reads_gating) ───────────────────


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


def _create_group(group_id: str, name: str) -> str:
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


def _add_membership(
    user_id: str, project_name: str, *, role: str = "operator",
) -> None:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project_name, user_id, role),
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


# The five router-admin routes that leaked. Each entry is
# (http_method, url_suffix) relative to the project resource root.
# ``victim`` is the project a low-priv caller must NOT be able to touch.
_ROUTES = [
    ("get", "/client-config"),
    ("get", "/installer"),
    ("post", "/stop"),
    ("get", "/aliases?alias=oldname"),
    ("delete", "/aliases/oldname"),
]


async def _call(client, method: str, url: str, cookie: str):
    fn = getattr(client, method)
    return await fn(
        url,
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


# ── Default-deny: a viewer of an UNRELATED project is 403 on every
#    route against the victim project (cross-tenant guard) ───────────


@pytest.mark.parametrize("method,suffix", _ROUTES)
async def test_cross_tenant_viewer_denied(
    aiohttp_client, router_app, router_module, register_project,
    method, suffix,
) -> None:
    # The victim owns a live agent token in the cache; a viewer of an
    # unrelated project must never be able to read/mutate it.
    register_project("victim")
    register_project("other")
    router_module._agent_token_cache["victim"] = (
        9.9e18, {"tok-secret": "Admin"},
    )
    viewer_id = _seed_user("vera", is_sysadmin=False)
    _add_membership(viewer_id, "other", role="viewer")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "vera")
    url = f"/agent-mcp/api/router/projects/victim{suffix}"
    resp = await _call(client, method, url, cookie)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"
    assert _CAP in body["message"]
    # And the live token never appears in the denied body.
    assert "tok-secret" not in (await resp.text())


# ── Sysadmin admits every route (regression guard) ─────────────────


@pytest.mark.parametrize("method,suffix", _ROUTES)
async def test_sysadmin_admits(
    aiohttp_client, router_app, router_module, register_project,
    method, suffix,
) -> None:
    register_project("victim")
    router_module._agent_token_cache["victim"] = (
        9.9e18, {"tok-secret": "Admin"},
    )
    _seed_user("root", is_sysadmin=True)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    url = f"/agent-mcp/api/router/projects/victim{suffix}"
    resp = await _call(client, method, url, cookie)

    # The gate must NOT be what stops a sysadmin. Handlers may still
    # 400/404 (e.g. unknown alias) but never 403.
    assert resp.status != 403, await resp.text()


# ── Cap delegation admits (Wave-9 shape) ───────────────────────────


async def test_delegated_cap_admits_client_config(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
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
    resp = await _call(
        client,
        "get",
        "/agent-mcp/api/router/projects/victim/client-config",
        cookie,
    )

    assert resp.status == 200, await resp.text()
