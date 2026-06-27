"""Login + logout view tests (Phase 1 PR C of prancy-napping-pie).

The router-level identity store (PR B) provides ``create_user``,
``create_session``, ``get_session`` and friends. PR C adds the HTTP
views that consume them:

  GET  /agent-mcp/login    — Jinja-rendered HTML login form
  POST /agent-mcp/login    — form-encoded username/password → cookie
  POST /agent-mcp/logout   — drop the session, clear the cookie

These tests assert cookie attributes (HttpOnly, SameSite=Lax,
Path=/agent-mcp/, Max-Age), redirect targets, the 401-on-bad-creds
path, and the session-resolver helpers' contract (``touch_session``
slides ``last_used_at``; ``resolve_current_user`` returns None for
expired sessions).
"""

from __future__ import annotations

import time

import pytest


pytestmark = pytest.mark.asyncio


# ── Helpers ─────────────────────────────────────────────────────────


def _identity_module():
    """Import (and reload-friendly) the identity module post-env-fixture.

    Ensures router.db migrations are applied so callers can freely
    insert users without depending on the aiohttp TestServer startup
    hook having fired first.
    """
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username: str = "alice", password: str = "hunter2") -> str:
    """Create a user via the identity module, return its user_id."""
    identity = _identity_module()
    return identity.create_user(username=username, password=password)


def _parse_set_cookie(set_cookie_header: str) -> dict[str, str]:
    """Tiny Set-Cookie parser sufficient for attribute assertions.

    Returns a dict like ``{"name": "agent_mcp_session", "value": "abc",
    "HttpOnly": "", "SameSite": "Lax", ...}``. Attribute-only flags
    are stored with empty-string values so callers can ``"HttpOnly" in
    parsed`` to check presence.
    """
    parts = [p.strip() for p in set_cookie_header.split(";")]
    name_val = parts[0].split("=", 1)
    result: dict[str, str] = {"name": name_val[0], "value": name_val[1] if len(name_val) > 1 else ""}
    for attr in parts[1:]:
        if "=" in attr:
            k, v = attr.split("=", 1)
            result[k.strip()] = v.strip()
        else:
            result[attr.strip()] = ""
    return result


# ── Login page rendering ───────────────────────────────────────────


async def test_login_page_renders(aiohttp_client, router_app) -> None:
    """GET /agent-mcp/login returns a 200 HTML page with a <form>.

    The wizard-redirect middleware would normally fire on an empty
    users table, but the /login route is exempt — operators in the
    "I've nuked my router.db" state can still see a useful page.
    Here we seed a user so the unconditional path is exercised.
    """
    _seed_user()
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/login")
    assert resp.status == 200, await resp.text()
    body = await resp.text()
    assert "<form" in body.lower()
    assert "username" in body.lower()
    assert "password" in body.lower()


async def test_login_success_sets_cookie_and_redirects(
    aiohttp_client, router_app,
) -> None:
    """Valid creds → 303 + Set-Cookie."""
    _seed_user(username="bob", password="bobpw")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "bob", "password": "bobpw"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert resp.headers.get("Location") == "/agent-mcp/"
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie, "expected Set-Cookie header on login success"
    parsed = _parse_set_cookie(set_cookie)
    assert parsed["name"] == "agent_mcp_session"
    assert parsed["value"]  # non-empty session id


async def test_login_failure_returns_401(aiohttp_client, router_app) -> None:
    """Wrong password → 401 with the form re-rendered + an error."""
    _seed_user(username="carol", password="rightpw")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "carol", "password": "wrongpw"},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()
    body = await resp.text()
    # Error must be visible to the user — exact copy isn't load-bearing
    # but some "invalid" string should appear.
    assert "invalid" in body.lower() or "incorrect" in body.lower()
    # No cookie set on failure.
    assert resp.headers.get("Set-Cookie") is None or "agent_mcp_session" not in resp.headers.get("Set-Cookie", "")


async def test_login_failure_unknown_user_returns_401(
    aiohttp_client, router_app,
) -> None:
    """Username doesn't exist → also 401, identical UX (no enumeration)."""
    _seed_user(username="dave", password="x")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "nobody", "password": "irrelevant"},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


# ── Logout ─────────────────────────────────────────────────────────


async def test_logout_clears_cookie(aiohttp_client, router_app) -> None:
    """POST /logout → cookie cleared + redirect to /login."""
    _seed_user(username="erin", password="erinpw")
    client = await aiohttp_client(router_app)
    # Log in first to get a session.
    login = await client.post(
        "/agent-mcp/login",
        data={"username": "erin", "password": "erinpw"},
        allow_redirects=False,
    )
    assert login.status == 303

    resp = await client.post("/agent-mcp/logout", allow_redirects=False)
    assert resp.status == 303
    assert resp.headers.get("Location") == "/agent-mcp/login"
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "agent_mcp_session" in set_cookie
    parsed = _parse_set_cookie(set_cookie)
    # Cleared cookie: empty value + Max-Age=0.
    assert parsed["value"] in ("", '""')
    assert parsed.get("Max-Age") == "0"


# ── GET /logout: bounce to /login (U002) ───────────────────────────


async def test_get_logout_redirects_to_login_303(
    aiohttp_client, router_app,
) -> None:
    """U002: GET /logout must 303 to /login, not 405 plain text.

    A user with a stale /logout bookmark must land on the login form,
    not see "405: Method Not Allowed". POST-only logout is the right
    CSRF defense; GET should be a pure redirect.
    """
    client = await aiohttp_client(router_app)
    resp = await client.get("/agent-mcp/logout", allow_redirects=False)
    assert resp.status == 303, await resp.text()
    assert resp.headers.get("Location") == "/agent-mcp/login"


async def test_get_logout_does_not_clear_session_cookie(
    aiohttp_client, router_app,
) -> None:
    """U002 follow-up: GET /logout must NOT clear the session cookie.

    Performing logout on GET would re-introduce CSRF risk (cross-site
    image tag could force-logout). GET /logout is a pure redirect.
    The cookie-clearing only happens on the explicit POST.
    """
    _seed_user(username="liam", password="liampw")
    client = await aiohttp_client(router_app)
    login = await client.post(
        "/agent-mcp/login",
        data={"username": "liam", "password": "liampw"},
        allow_redirects=False,
    )
    assert login.status == 303

    resp = await client.get("/agent-mcp/logout", allow_redirects=False)
    assert resp.status == 303
    # No Set-Cookie on GET — or if one is somehow present, it must not be
    # the session-clearing one. Both shapes are acceptable.
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "Max-Age=0" not in set_cookie, (
        "GET /logout must not clear session (CSRF defense)"
    )


async def test_post_logout_still_works_after_get_handler_added(
    aiohttp_client, router_app,
) -> None:
    """U002 regression guard: POST /logout still 303s + clears cookie.

    Adding the GET handler must not break the POST path; the POST is the
    only one that actually drops the session and clears the cookie.
    """
    _seed_user(username="mona", password="monapw")
    client = await aiohttp_client(router_app)
    login = await client.post(
        "/agent-mcp/login",
        data={"username": "mona", "password": "monapw"},
        allow_redirects=False,
    )
    assert login.status == 303

    resp = await client.post("/agent-mcp/logout", allow_redirects=False)
    assert resp.status == 303
    assert resp.headers.get("Location") == "/agent-mcp/login"
    assert "Max-Age=0" in resp.headers.get("Set-Cookie", "")


# ── Cookie attribute coverage ──────────────────────────────────────


async def test_session_cookie_attributes(aiohttp_client, router_app) -> None:
    """HttpOnly + SameSite=Lax + Path=/agent-mcp/ + Max-Age=2592000."""
    _seed_user(username="frank", password="frankpw")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "frank", "password": "frankpw"},
        allow_redirects=False,
    )
    assert resp.status == 303
    set_cookie = resp.headers.get("Set-Cookie", "")
    parsed = _parse_set_cookie(set_cookie)
    assert "HttpOnly" in parsed
    assert parsed.get("SameSite") == "Lax"
    assert parsed.get("Path") == "/agent-mcp/"
    # 30 days = 2592000 seconds.
    assert parsed.get("Max-Age") == "2592000"


# ── Session resolver + touch_session ───────────────────────────────


async def test_session_last_used_at_slides(aiohttp_client, router_app) -> None:
    """Two requests with the same cookie → last_used_at advances."""
    _seed_user(username="gail", password="gailpw")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "gail", "password": "gailpw"},
        allow_redirects=False,
    )
    set_cookie = resp.headers.get("Set-Cookie", "")
    # Pull the value out of the Set-Cookie header.
    sid = set_cookie.split(";", 1)[0].split("=", 1)[1]

    identity = _identity_module()
    first = identity.get_session(sid)
    assert first is not None
    time.sleep(0.02)

    # touch_session is the helper PR C exposes for the session-resolver
    # path; should slide the timestamp the same way get_session does.
    from agent_mcp.router import login as login_views

    login_views.touch_session(sid)

    with identity._connect() as conn:
        row = conn.execute(
            "SELECT last_used_at FROM sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()
    assert row is not None
    assert row["last_used_at"] >= first["last_used_at"]
    assert row["last_used_at"] != first["last_used_at"]


async def test_resolve_current_user_returns_none_for_expired(
    aiohttp_client, router_app,
) -> None:
    """The resolver helper returns None for an expired session."""
    identity = _identity_module()
    uid = identity.create_user(username="hank", password="x")
    sid = identity.create_session(uid, lifetime_days=-1)

    from aiohttp.test_utils import make_mocked_request
    from agent_mcp.router import login as login_views

    req = make_mocked_request(
        "GET",
        "/agent-mcp/",
        headers={"Cookie": f"agent_mcp_session={sid}"},
    )
    assert login_views.resolve_current_user(req) is None


async def test_resolve_current_user_returns_user_for_valid_session(
    aiohttp_client, router_app,
) -> None:
    """The resolver returns the user dict for a valid session cookie."""
    identity = _identity_module()
    uid = identity.create_user(username="iris", password="x")
    sid = identity.create_session(uid)

    from aiohttp.test_utils import make_mocked_request
    from agent_mcp.router import login as login_views

    req = make_mocked_request(
        "GET",
        "/agent-mcp/",
        headers={"Cookie": f"agent_mcp_session={sid}"},
    )
    user = login_views.resolve_current_user(req)
    assert user is not None
    assert user["user_id"] == uid
    assert user["username"] == "iris"


async def test_resolve_current_user_no_cookie_returns_none(
    aiohttp_client, router_app,
) -> None:
    """No cookie → None."""
    from aiohttp.test_utils import make_mocked_request
    from agent_mcp.router import login as login_views

    req = make_mocked_request("GET", "/agent-mcp/")
    assert login_views.resolve_current_user(req) is None


# ── next= redirect param ───────────────────────────────────────────


async def test_login_honours_safe_next_param(
    aiohttp_client, router_app,
) -> None:
    """A path-relative ?next= under /agent-mcp/ is honoured."""
    _seed_user(username="jane", password="x")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login?next=/agent-mcp/app/foo/",
        data={"username": "jane", "password": "x"},
        allow_redirects=False,
    )
    assert resp.status == 303
    assert resp.headers.get("Location") == "/agent-mcp/app/foo/"


async def test_login_rejects_open_redirect(
    aiohttp_client, router_app,
) -> None:
    """A protocol-relative or off-host next= falls back to /agent-mcp/."""
    _seed_user(username="kyle", password="x")
    client = await aiohttp_client(router_app)
    for nxt in ("//evil.example.com/", "https://evil.example.com/", "/no-prefix"):
        resp = await client.post(
            f"/agent-mcp/login?next={nxt}",
            data={"username": "kyle", "password": "x"},
            allow_redirects=False,
        )
        assert resp.status == 303
        assert resp.headers.get("Location") == "/agent-mcp/", (
            f"next={nxt!r} should be rejected, got Location={resp.headers.get('Location')!r}"
        )
        # New session each iteration is fine for the assertion shape.
