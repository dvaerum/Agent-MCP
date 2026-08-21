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

import asyncio

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


@pytest.mark.no_auth_seed_session
async def test_login_unknown_user_runs_equal_argon2_work(
    aiohttp_client, router_app, monkeypatch,
) -> None:
    """Missing-user login must run a dummy argon2 verify (timing enum).

    The enumeration mitigation is same-status + same-copy, but that is
    defeated if the missing-user branch skips argon2 entirely: an
    existing user costs ~argon2-verify, a nonexistent one is near-free,
    a >10x timing gap. Both branches must invoke ``verify_password``
    exactly once so the response timing carries no signal about whether
    the username exists.
    """
    import agent_mcp.router.identity as identity
    from agent_mcp.router import login as login_views

    _seed_user(username="realuser", password="realpw")

    calls: list[tuple[str, str]] = []
    real_verify = identity.verify_password

    def _spy(hashed: str, password: str) -> bool:
        calls.append((hashed, password))
        return real_verify(hashed, password)

    monkeypatch.setattr(identity, "verify_password", _spy)

    client = await aiohttp_client(router_app)

    # Existing user, wrong password → argon2 verify runs once.
    r_existing = await client.post(
        "/agent-mcp/login",
        data={"username": "realuser", "password": "wrong"},
        allow_redirects=False,
    )
    assert r_existing.status == 401, await r_existing.text()
    assert len(calls) == 1, "existing-user path must run argon2 verify once"
    body_existing = await r_existing.text()

    calls.clear()

    # Nonexistent user → argon2 verify STILL runs once, against the
    # fixed decoy hash (equal work, no timing signal).
    r_missing = await client.post(
        "/agent-mcp/login",
        data={"username": "ghost-does-not-exist", "password": "wrong"},
        allow_redirects=False,
    )
    assert r_missing.status == 401, await r_missing.text()
    assert len(calls) == 1, (
        "missing-user path must run a dummy argon2 verify to equalise timing"
    )
    # The dummy verify runs against the constant decoy hash, not a real
    # user's stored hash.
    assert calls[0][0] == login_views._DECOY_PASSWORD_HASH
    body_missing = await r_missing.text()

    # Identical 401 + identical error copy → no content-based signal
    # either. (The echoed username differs, as it is attacker-supplied
    # and reveals nothing; the error message must match.)
    assert "Invalid username or password." in body_existing
    assert "Invalid username or password." in body_missing


# ── Login CSRF: Origin / Sec-Fetch-Site validation (R9-F1) ─────────
#
# POST /login MINTS a session cookie; it does not consume one. So
# SameSite=Lax (which only stops the *authenticated* cookie from riding
# a cross-site request) gives this endpoint no protection at all — an
# attacker page can auto-submit a cross-site login form with the
# ATTACKER's creds and silently log the victim into the attacker's
# account (login CSRF / session forcing). The server therefore rejects a
# cross-site Origin before minting anything.


async def test_login_rejects_cross_site_origin(
    aiohttp_client, router_app,
) -> None:
    """Cross-site Origin → 403, NO session cookie minted (R9-F1)."""
    _seed_user(username="csrf_bob", password="csrf-bob-pw")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "csrf_bob", "password": "csrf-bob-pw"},
        headers={"Origin": "https://evil.example"},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie is None or "agent_mcp_session" not in set_cookie, (
        "cross-site login must NOT mint a session cookie"
    )


async def test_login_rejects_cross_site_sec_fetch_site(
    aiohttp_client, router_app,
) -> None:
    """Sec-Fetch-Site: cross-site (no Origin) → 403, no cookie (R9-F1)."""
    _seed_user(username="csrf_sfs", password="csrf-sfs-pw")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "csrf_sfs", "password": "csrf-sfs-pw"},
        headers={"Sec-Fetch-Site": "cross-site"},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie is None or "agent_mcp_session" not in set_cookie


async def test_login_accepts_same_origin(
    aiohttp_client, router_app,
) -> None:
    """A same-origin Origin (matching host) → normal 303 + cookie."""
    _seed_user(username="csrf_same", password="csrf-same-pw")
    client = await aiohttp_client(router_app)
    same_origin = str(client.make_url("/agent-mcp/login").origin())
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "csrf_same", "password": "csrf-same-pw"},
        headers={"Origin": same_origin, "Sec-Fetch-Site": "same-origin"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert "agent_mcp_session" in resp.headers.get("Set-Cookie", "")


async def test_login_accepts_no_origin_curl(
    aiohttp_client, router_app,
) -> None:
    """No Origin + no Sec-Fetch-Site (curl / CLI / pentest harness) →
    allowed. These are not browser-driven and carry no CSRF risk."""
    _seed_user(username="csrf_curl", password="csrf-curl-pw")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "csrf_curl", "password": "csrf-curl-pw"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert "agent_mcp_session" in resp.headers.get("Set-Cookie", "")


# ── OBS7: X-Forwarded-Host trust gated on a trusted-proxy source ────
#
# ``_external_origin`` derives the router's OWN external origin so
# ``enforce_same_origin`` can compare it against the request ``Origin``.
# Behind a reverse proxy the transport host/scheme reflect the loopback
# hop, so the proxy's ``X-Forwarded-Host`` / ``X-Forwarded-Proto`` carry
# the external values. But those headers are client-settable: if we trust
# them from an UNTRUSTED peer, a request with ``Origin: http://evil`` +
# ``X-Forwarded-Host: evil`` makes the computed self-origin EQUAL the
# attacker origin, so the same-origin check passes. Gate XFH/XFP trust on
# the direct peer being a trusted proxy (loopback / UDS by default, the
# real nginx-on-loopback tailnet posture). Not browser-exploitable today
# (a cross-site form can't set headers; a header-setting fetch is
# CORS-preflight-blocked; a victim's browser sends a truthful Origin and
# no attacker XFH), but a latent robustness gap. See OBS7.


def _mocked_request(remote: str | None, headers: dict[str, str]):
    """A POST /login request with a chosen peer IP and headers.

    ``remote=None`` models a UDS / in-process peer (empty
    ``request.remote``) which the trusted-proxy check treats as trusted
    loopback. A dotted-quad models a direct (untrusted) client hit.
    """
    from unittest import mock

    from aiohttp.test_utils import make_mocked_request

    transport = mock.Mock()
    peername = (remote, 40000) if remote else None
    transport.get_extra_info = lambda key, default=None: (
        peername if key == "peername" else default
    )
    return make_mocked_request(
        "POST", "/agent-mcp/login", headers=headers, transport=transport,
    )


async def test_external_origin_trusts_xfh_from_trusted_proxy() -> None:
    """Trusted-proxy peer → XFH/XFP ARE honoured (preserves the real
    tailnet-behind-nginx deployment: login must keep working)."""
    from agent_mcp.router import login

    req = _mocked_request(
        "127.0.0.1",
        {
            "Host": "loopback:1337",
            "X-Forwarded-Host": "agent.tailnet.ts.net",
            "X-Forwarded-Proto": "https",
        },
    )
    assert login._external_origin(req) == "https://agent.tailnet.ts.net"


async def test_external_origin_trusts_xfh_from_uds_peer() -> None:
    """UDS / empty-remote peer (Unix-socket reverse proxy) is trusted
    loopback → XFH honoured."""
    from agent_mcp.router import login

    req = _mocked_request(
        None,
        {
            "Host": "loopback",
            "X-Forwarded-Host": "agent.tailnet.ts.net",
            "X-Forwarded-Proto": "https",
        },
    )
    assert login._external_origin(req) == "https://agent.tailnet.ts.net"


async def test_external_origin_ignores_xfh_from_untrusted_source() -> None:
    """Untrusted direct peer → a forged XFH/XFP is IGNORED; the real
    transport host/scheme win (the OBS7 hardening)."""
    from agent_mcp.router import login

    req = _mocked_request(
        "203.0.113.7",
        {
            "Host": "real.host:1337",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
        },
    )
    assert login._external_origin(req) == "http://real.host:1337"


async def test_enforce_same_origin_rejects_forged_xfh_from_untrusted() -> None:
    """The exploit shape: untrusted peer sends Origin: http://evil +
    X-Forwarded-Host: evil. The forged XFH no longer makes the computed
    self-origin equal the attacker origin, so the same-origin check
    rejects it with 403."""
    from aiohttp import web

    from agent_mcp.router import login

    req = _mocked_request(
        "203.0.113.7",
        {
            "Host": "real.host:1337",
            "Origin": "http://evil.example",
            "X-Forwarded-Host": "evil.example",
        },
    )
    with pytest.raises(web.HTTPForbidden):
        login.enforce_same_origin(req)


async def test_enforce_same_origin_accepts_proxy_forwarded_origin() -> None:
    """Trusted proxy forwards the external Origin + XFH; they match, so
    the same-origin check passes (tailnet login preserved)."""
    from agent_mcp.router import login

    req = _mocked_request(
        "127.0.0.1",
        {
            "Host": "loopback:1337",
            "Origin": "https://agent.tailnet.ts.net",
            "X-Forwarded-Host": "agent.tailnet.ts.net",
            "X-Forwarded-Proto": "https",
        },
    )
    # Returns None (no raise) when same-origin.
    assert login.enforce_same_origin(req) is None


async def test_cookie_secure_flag_ignores_forged_xfp_from_untrusted() -> None:
    """A forged X-Forwarded-Proto: https from an untrusted peer must not
    drive the Secure decision; the real (http) transport scheme wins."""
    from agent_mcp.router import login

    req = _mocked_request(
        "203.0.113.7",
        {"Host": "real.host", "X-Forwarded-Proto": "https"},
    )
    assert login.cookie_secure_flag(req) is False


async def test_cookie_secure_flag_honours_xfp_from_trusted_proxy() -> None:
    """XFP from a trusted proxy still drives Secure (tailnet TLS)."""
    from agent_mcp.router import login

    req = _mocked_request(
        "127.0.0.1",
        {"Host": "loopback", "X-Forwarded-Proto": "https"},
    )
    assert login.cookie_secure_flag(req) is True


async def test_sso_cookie_secure_flag_ignores_forged_xfp_from_untrusted() -> None:
    """R6-F3 (pentest-all round 6, class-sweep miss of R6-F1/OBS7): the
    sso.py flow-cookie's own local copy of the cookie-secure heuristic
    must not honour X-Forwarded-Proto from an untrusted peer either —
    same rule as login.cookie_secure_flag, the docstring already claims
    this but the code never actually gated it before this fix."""
    from agent_mcp.router import sso

    req = _mocked_request(
        "203.0.113.7",
        {"Host": "real.host", "X-Forwarded-Proto": "https"},
    )
    assert sso._cookie_secure_flag(req) is False


async def test_sso_cookie_secure_flag_honours_xfp_from_trusted_proxy() -> None:
    """XFP from a trusted proxy still drives Secure (tailnet TLS)."""
    from agent_mcp.router import sso

    req = _mocked_request(
        "127.0.0.1",
        {"Host": "loopback", "X-Forwarded-Proto": "https"},
    )
    assert sso._cookie_secure_flag(req) is True


async def test_sso_default_redirect_url_ignores_forged_xfh_from_untrusted() -> None:
    """The sibling XFH-trust site: sso._default_redirect_url honours XFH
    only from a trusted proxy (IdP exact-match already backstops it, but
    we gate consistently)."""
    from agent_mcp.router import sso

    untrusted = _mocked_request(
        "203.0.113.7",
        {"Host": "real.host", "X-Forwarded-Host": "evil.example",
         "X-Forwarded-Proto": "https"},
    )
    assert sso._default_redirect_url(untrusted) == (
        "http://real.host/agent-mcp/sso/callback"
    )
    trusted = _mocked_request(
        "127.0.0.1",
        {"Host": "loopback", "X-Forwarded-Host": "agent.tailnet.ts.net",
         "X-Forwarded-Proto": "https"},
    )
    assert sso._default_redirect_url(trusted) == (
        "https://agent.tailnet.ts.net/agent-mcp/sso/callback"
    )


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
    await asyncio.sleep(0.02)

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
    """A protocol-relative or off-host next= falls back to the mount root.

    ADR-0020: `next` is now validated as any SAME-ORIGIN absolute path
    (root paths like `/app/…` are legitimate mounts), not pinned to
    `/agent-mcp/`. Genuine open-redirect vectors (protocol-relative,
    off-host) are still rejected."""
    _seed_user(username="kyle", password="x")
    client = await aiohttp_client(router_app)
    # Open-redirect vectors → rejected to the (tailnet) mount root.
    for nxt in ("//evil.example.com/", "https://evil.example.com/"):
        resp = await client.post(
            f"/agent-mcp/login?next={nxt}",
            data={"username": "kyle", "password": "x"},
            allow_redirects=False,
        )
        assert resp.status == 303
        assert resp.headers.get("Location") == "/agent-mcp/", (
            f"next={nxt!r} should be rejected, got Location={resp.headers.get('Location')!r}"
        )
    # A same-origin path OUTSIDE /agent-mcp/ (e.g. a root-mount deep link)
    # is now honoured — it can't leave the origin, so it's not an
    # open-redirect.
    resp = await client.post(
        "/agent-mcp/login?next=/app/foo",
        data={"username": "kyle", "password": "x"},
        allow_redirects=False,
    )
    assert resp.status == 303
    assert resp.headers.get("Location") == "/app/foo", (
        f"same-origin next should be honoured, got {resp.headers.get('Location')!r}"
    )
