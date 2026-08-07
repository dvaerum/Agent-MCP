"""Setup-wizard tests (Phase 1 PR C of prancy-napping-pie).

Wires the empty-users redirect middleware + the GET/POST /setup
handlers. Mirrors PR B's identity-store invariants:

  * empty users table → ANY path under /agent-mcp/ (except /setup
    and /static) redirects to /agent-mcp/setup
  * /setup is reachable only while users table is empty;
    once a user exists, /setup itself 303-redirects to /login
  * the first user created via the wizard is implicitly granted
    membership in every pre-existing project (PR B contract)
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.no_seed_operator]


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


# ── Empty-users middleware ─────────────────────────────────────────


async def test_empty_users_redirects_to_setup(
    aiohttp_client, router_app,
) -> None:
    """Visiting any /agent-mcp/ path with empty users → 303 /setup."""
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/", allow_redirects=False)
    assert resp.status == 303
    assert resp.headers.get("Location") == "/agent-mcp/setup"


async def test_login_page_redirected_to_setup_when_empty(
    aiohttp_client, router_app,
) -> None:
    """Even /login redirects when no users exist — the operator's first
    action MUST be account creation, not a doomed login attempt."""
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/login", allow_redirects=False)
    assert resp.status == 303
    assert resp.headers.get("Location") == "/agent-mcp/setup"


async def test_static_assets_not_redirected(
    aiohttp_client, router_app, router_env,
) -> None:
    """Static-asset paths must NOT redirect; the wizard's own CSS would
    break otherwise. With no asset matching, we expect a 404, not a 303."""
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/assets/missing.css", allow_redirects=False,
    )
    assert resp.status != 303
    assert resp.status == 404


async def test_internal_json_apis_not_redirected(
    aiohttp_client, router_app,
) -> None:
    """Machine-to-machine surfaces (/api/, /mcp/) must NOT redirect;
    they're hit by agents, dashboard fetch() calls, and CI
    integrations that have no business rendering a wizard. The public
    ``/api/router/health`` descriptor is JSON — should be 200 even
    on a brand-new deploy with no users."""
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/router/health",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["ok"] is True


# ── Setup page rendering ───────────────────────────────────────────


async def test_setup_page_renders(aiohttp_client, router_app) -> None:
    """GET /setup with empty users table → 200 + form."""
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/setup")
    assert resp.status == 200, await resp.text()
    body = await resp.text()
    assert "<form" in body.lower()
    assert "username" in body.lower()
    assert "password" in body.lower()


async def test_setup_blocked_when_users_exist(
    aiohttp_client, router_app,
) -> None:
    """Pre-seed a user → GET /setup → 303 to /login."""
    identity = _identity_module()
    identity.create_user(username="existing", password="x")
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/setup", allow_redirects=False)
    assert resp.status == 303
    assert resp.headers.get("Location") == "/agent-mcp/login"


# ── Setup POST: happy path ─────────────────────────────────────────


async def test_setup_creates_user_and_logs_in(
    aiohttp_client, router_app,
) -> None:
    """Valid POST /setup → user created, session cookie set, redirect /."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "first_op",
            # ≥12 chars to satisfy the password-strength policy (AC-2).
            "password": "secret-pw-1234",
            "password_confirm": "secret-pw-1234",
        },
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert resp.headers.get("Location") == "/agent-mcp/"
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "agent_mcp_session" in set_cookie

    identity = _identity_module()
    user = identity.get_user_by_username("first_op")
    assert user is not None
    assert identity.verify_password(user["password_hash"], "secret-pw-1234")


async def test_setup_accepts_optional_email(
    aiohttp_client, router_app,
) -> None:
    """An email field, when supplied, is persisted on the user row."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "with_email",
            # ≥12 chars to satisfy the password-strength policy (AC-2).
            "password": "ops-password-1",
            "password_confirm": "ops-password-1",
            "email": "ops@example.com",
        },
        allow_redirects=False,
    )
    assert resp.status == 303

    identity = _identity_module()
    user = identity.get_user_by_username("with_email")
    assert user is not None
    assert user["email"] == "ops@example.com"


# ── Setup POST: failure modes ──────────────────────────────────────


async def test_password_mismatch_rejected(
    aiohttp_client, router_app,
) -> None:
    """Passwords don't match → form re-rendered + no user created."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "mismatch",
            "password": "a",
            "password_confirm": "b",
        },
        allow_redirects=False,
    )
    assert resp.status == 400, await resp.text()
    body = await resp.text()
    assert "match" in body.lower() or "mismatch" in body.lower()

    identity = _identity_module()
    assert identity.get_user_by_username("mismatch") is None


async def test_empty_username_rejected(
    aiohttp_client, router_app,
) -> None:
    """Empty username → 400, no user, error visible."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "",
            "password": "x",
            "password_confirm": "x",
        },
        allow_redirects=False,
    )
    assert resp.status == 400


async def test_setup_post_blocked_when_users_exist(
    aiohttp_client, router_app,
) -> None:
    """A POST to /setup after a user exists is rejected — even if
    the form is replayed (e.g. browser back button)."""
    identity = _identity_module()
    identity.create_user(username="already_there", password="x")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "sneaky",
            "password": "x",
            "password_confirm": "x",
        },
        allow_redirects=False,
    )
    # 303 to /login (we silently bounce; alternative would be 409 but
    # the UX win is one fewer error page) — accept either as long as
    # no user was created.
    assert resp.status in (303, 409)
    assert _identity_module().get_user_by_username("sneaky") is None


# ── Setup CSRF: Origin / Sec-Fetch-Site validation (R9-F1) ─────────
#
# The setup POST also MINTS a session cookie (bootstrapping the first
# operator). It is currently gated by the users-empty check so the
# cross-site window is first-boot only, but it belongs to the same
# cookie-minting class as /login and gets the same Origin guard so a
# future refactor of the empty-check can't silently reopen it.


async def test_setup_rejects_cross_site_origin(
    aiohttp_client, router_app,
) -> None:
    """Cross-site Origin on /setup (users-empty) → 403, no user, no
    cookie (R9-F1)."""
    _identity_module()  # migrate so the users table exists + is empty
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "evil_first_op",
            "password": "secret-pw-1234",
            "password_confirm": "secret-pw-1234",
        },
        headers={"Origin": "https://evil.example"},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie is None or "agent_mcp_session" not in set_cookie
    assert _identity_module().get_user_by_username("evil_first_op") is None


async def test_setup_rejects_cross_site_sec_fetch_site(
    aiohttp_client, router_app,
) -> None:
    """Sec-Fetch-Site: cross-site (no Origin) on /setup → 403, no user."""
    _identity_module()
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "evil_sfs_op",
            "password": "secret-pw-1234",
            "password_confirm": "secret-pw-1234",
        },
        headers={"Sec-Fetch-Site": "cross-site"},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    assert _identity_module().get_user_by_username("evil_sfs_op") is None


async def test_setup_accepts_same_origin(
    aiohttp_client, router_app,
) -> None:
    """Same-origin Origin on /setup → normal 303 + user created."""
    _identity_module()
    client = await aiohttp_client(router_app)
    same_origin = str(client.make_url("/agent-mcp/setup").origin())
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "good_first_op",
            "password": "secret-pw-1234",
            "password_confirm": "secret-pw-1234",
        },
        headers={"Origin": same_origin, "Sec-Fetch-Site": "same-origin"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert "agent_mcp_session" in resp.headers.get("Set-Cookie", "")
    assert _identity_module().get_user_by_username("good_first_op") is not None


async def test_setup_accepts_no_origin_curl(
    aiohttp_client, router_app,
) -> None:
    """No Origin / no Sec-Fetch-Site (curl / CLI) on /setup → allowed."""
    _identity_module()
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "curl_first_op",
            "password": "secret-pw-1234",
            "password_confirm": "secret-pw-1234",
        },
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert "agent_mcp_session" in resp.headers.get("Set-Cookie", "")
    assert _identity_module().get_user_by_username("curl_first_op") is not None
