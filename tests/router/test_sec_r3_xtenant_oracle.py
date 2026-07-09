"""Round 3 — PF-1: cross-tenant project-existence oracle.

Before this fix the per-project membership gate in
``require_operator_session_middleware`` answered a project the
authenticated operator wasn't a member of differently from a project
that didn't exist at all:

  * ``GET /agent-mcp/api/<existing-not-member>/…`` → 401 whose body
    reflected the canonical project name.
  * ``GET /agent-mcp/api/<nonexistent>/…``         → 404 "unknown
    project".
  * ``GET /agent-mcp/app/<existing-not-member>/``  → 401.
  * ``GET /agent-mcp/app/<nonexistent>/``          → 200 (SPA shell).

That status+body differential is an oracle: ANY authenticated user
(even a viewer of one unrelated project) could brute-force the
lowercase-slug space and enumerate other tenants' project names. It
is the same class SEC5 closed on ``/mcp`` (uniform floored response
for existing-not-member and nonexistent alike), and it contradicted
``_project_exists``'s own docstring.

The fix collapses existing-not-member onto the SAME wire response a
nonexistent slug yields, on BOTH surfaces, while leaving genuine
members and the unauthenticated 401 path untouched. These tests pin
that indistinguishability by asserting byte-identical
(status, body) pairs and the absence of the project name in the
response body.
"""

from __future__ import annotations

import pytest


pytestmark = [
    pytest.mark.asyncio,
    # Seed our own users + log in explicitly so the assertion targets
    # the real membership seam, not the auto-login sentinel fixture.
    pytest.mark.no_auth_seed_session,
]


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}

# A project that IS registered but that our test operator is NOT a
# member of, and one that is never registered at all.
_EXISTING = "existing-tenant-proj"
_NONEXISTENT = "ghost-tenant-proj"


# ── Helpers (mirrors test_p3_perm_overhaul) ─────────────────────────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username: str, password: str = "passwordpassword") -> str:
    """Create a non-sysadmin user. The FIRST user a router ever sees
    is implicitly sysadmin, so ensure somebody else took that slot."""
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
    return identity.create_user(username=username, password=password)


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
    name, _, value = set_cookie.split(";", 1)[0].partition("=")
    assert name.strip() == "agent_mcp_session"
    return value.strip()


# ── REST /api/<project>/ — the oracle surface ───────────────────────


async def test_api_existing_nonmember_matches_nonexistent_strict_accept(
    aiohttp_client, router_app, register_project,
) -> None:
    """As a non-member operator, an EXISTING project and a NONEXISTENT
    one must return the identical (status, body) with the strict API
    Accept header — and the body must not leak the project name."""
    register_project(_EXISTING)
    _seed_user("mallory")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "mallory")

    existing = await client.get(
        f"/agent-mcp/api/{_EXISTING}/agents",
        headers=_STRICT_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    existing_body = await existing.text()
    ghost = await client.get(
        f"/agent-mcp/api/{_NONEXISTENT}/agents",
        headers=_STRICT_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    ghost_body = await ghost.text()

    assert existing.status == ghost.status, (
        f"status oracle: existing={existing.status} ghost={ghost.status}"
    )
    assert existing.status == 404, existing_body
    assert existing_body == ghost_body, "body oracle (strict accept)"
    assert _EXISTING not in existing_body, "project name leaked in body"


async def test_api_existing_nonmember_matches_nonexistent_no_accept(
    aiohttp_client, router_app, register_project,
) -> None:
    """Same indistinguishability must hold WITHOUT the strict Accept
    header: a nonexistent project trips the 406 Accept-version gate,
    so an existing-not-member one must trip the SAME 406 — otherwise
    the 406-vs-404 differential is itself an oracle."""
    register_project(_EXISTING)
    _seed_user("mallory")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "mallory")

    existing = await client.get(
        f"/agent-mcp/api/{_EXISTING}/agents",
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    existing_body = await existing.text()
    ghost = await client.get(
        f"/agent-mcp/api/{_NONEXISTENT}/agents",
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    ghost_body = await ghost.text()

    assert existing.status == ghost.status, (
        f"status oracle: existing={existing.status} ghost={ghost.status}"
    )
    assert existing.status == 406, existing_body
    assert existing_body == ghost_body, "body oracle (no accept)"
    assert _EXISTING not in existing_body, "project name leaked in body"


# ── /app/<project>/ — the starker (401 vs 200) differential ─────────


async def test_app_existing_nonmember_matches_nonexistent(
    aiohttp_client, router_app, register_project, write_dashboard_file,
) -> None:
    """On the ``/app/`` surface a nonexistent slug already serves the
    static SPA shell (200). A non-member hitting an EXISTING project
    must get the identical response — not a 401 that betrays the
    project's existence."""
    write_dashboard_file("index.html", "<html><body>SPA SHELL</body></html>")
    register_project(_EXISTING)
    _seed_user("mallory")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "mallory")

    existing = await client.get(
        f"/agent-mcp/app/{_EXISTING}/",
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    existing_body = await existing.text()
    ghost = await client.get(
        f"/agent-mcp/app/{_NONEXISTENT}/",
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    ghost_body = await ghost.text()

    assert existing.status == ghost.status, (
        f"status oracle: existing={existing.status} ghost={ghost.status}"
    )
    assert existing.status == 200, existing_body
    assert existing_body == ghost_body, "body oracle (/app SPA shell)"
    assert _EXISTING not in existing_body, "project name leaked in body"


# ── Regression: genuine members + unauth path unaffected ────────────


async def test_member_still_reaches_api_project(
    aiohttp_client, router_app, register_project,
) -> None:
    """A real member must still be routed to their project — the
    collapse to 404 must not swallow legitimate access. The backend
    isn't running in unit tests, so a 5xx is expected; we only assert
    the auth gate admitted the caller (never 401/403/404/406)."""
    register_project(_EXISTING)
    member_id = _seed_user("olive")
    _add_membership(member_id, _EXISTING, role="operator")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "olive")

    resp = await client.get(
        f"/agent-mcp/api/{_EXISTING}/agents",
        headers=_STRICT_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status not in (401, 403, 404, 406), await resp.text()


async def test_member_still_reaches_app_project(
    aiohttp_client, router_app, register_project, write_dashboard_file,
) -> None:
    """A real member reaches the ``/app/`` SPA shell normally (200)."""
    write_dashboard_file("index.html", "<html><body>SPA SHELL</body></html>")
    register_project(_EXISTING)
    member_id = _seed_user("olive")
    _add_membership(member_id, _EXISTING, role="operator")
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "olive")

    resp = await client.get(
        f"/agent-mcp/app/{_EXISTING}/",
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()


async def test_unauth_uniform_401_pre_resolution(
    aiohttp_client, router_app, register_project,
) -> None:
    """An UNauthenticated caller still hits the uniform pre-resolution
    401 for both existing and nonexistent projects — the cookie gate
    fires before any project lookup, so that path was already
    oracle-free and must stay so."""
    register_project(_EXISTING)
    client = await aiohttp_client(router_app)

    existing = await client.get(
        f"/agent-mcp/api/{_EXISTING}/agents",
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )
    existing_body = await existing.text()
    ghost = await client.get(
        f"/agent-mcp/api/{_NONEXISTENT}/agents",
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )
    ghost_body = await ghost.text()

    assert existing.status == 401
    assert existing.status == ghost.status
    assert existing_body == ghost_body, "unauth 401 must be uniform"
    assert _EXISTING not in existing_body
