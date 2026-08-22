"""R9-F4 (HIGH, live-exploited) — ``revalidate_capability_or_403`` re-
derives capability + project membership from a fresh DB read, but
re-derives the CALLER'S IDENTITY from ``req.get("user")`` — the value
``require_operator_session_middleware`` cached ONCE at request entry,
BEFORE the handler's genuine yield point. It never re-checks that the
underlying SESSION is still valid. A session invalidated (logged out)
DURING the yield point is invisible to the revalidation — the
privileged write still completes using a session that's already been
logged out.

Live-exploited repro (round-9 fix-regression pentest lane): logged in
as ``dev``, opened a slow-drip PATCH rename (all but last body byte
held), fired a real ``POST /agent-mcp/logout`` on a SEPARATE
connection using the SAME session cookie mid-drip (confirmed 303 +
session genuinely deleted server-side), then sent the final body
byte -> pre-fix 200 OK (rename completed using a session that had
already been logged out).

Fix: ``revalidate_capability_or_403`` now re-runs
``login.resolve_current_user`` against the request's live session
cookie (the exact same live DB lookup the entry-time middleware gate
uses) and denies with the same 403 shape when the session no longer
resolves — in addition to the existing capability/group
re-derivation. This lives in the ONE shared helper, so every caller
(``admin_users_api`` user/group/membership/role-change routes,
``admin_api.create_project_handler``, and the R8-F3 combo helper
which calls through to this function) inherits the fix transitively.

The tests below reproduce the race DETERMINISTICALLY — no real
sleeps. A monkeypatched ``_json_body`` pauses mid-read on an
``asyncio.Event``; a REAL concurrent ``POST /agent-mcp/logout`` (same
session cookie, separate connection) commits while it's paused; the
read is then released and the handler resumes. Mirrors the
``test_sec_r8f3_project_membership_toctou.py`` / ``test_sec_r6f2_
stale_principal_toctou.py`` idiom.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror test_sec_r6f2_stale_principal_toctou.py) ────────


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
        is_empty = conn.execute(
            "SELECT COUNT(*) AS n FROM users"
        ).fetchone()["n"] == 0
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


async def _login(client, username: str, password: str = "passwordpassword") -> str:
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


def _session_row_count(session_id: str) -> int:
    identity = _identity_module()
    with identity._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]


# ── Test A: the LIVE-EXPLOITED repro — logout-during-slow-drip-PATCH ─


@pytest.mark.no_auth_seed_session
async def test_slow_drip_edit_user_rejects_logged_out_session(
    aiohttp_client, router_app, monkeypatch,
) -> None:
    """A slow-drip PATCH must re-check that the caller's SESSION is
    still live after the body-read yield point, not just re-derive
    capability/group membership off the cached ``req['user']`` id.
    ``dev`` (sysadmin) opens the edit on ``victim``; while dev's
    body-read is paused, a SEPARATE connection logs dev's own session
    out via the real ``POST /agent-mcp/logout`` endpoint (same
    cookie) — the session row is genuinely deleted server-side before
    dev's paused request resumes. Pre-fix this still 200s (the
    logged-out session's write completes); post-fix it must 403 and
    the edit must not have landed.
    """
    from agent_mcp.router import admin_users_api

    _seed_user("dev-r9f4", is_sysadmin=True)
    victim_id = _seed_user("victim-r9f4", is_sysadmin=False)

    client = await aiohttp_client(router_app)
    dev_cookie = await _login(client, "dev-r9f4")

    # Separate connection, SAME session cookie — mirrors the live
    # repro's "separate connection, same cookie" logout.
    logout_client = await aiohttp_client(router_app)

    assert _session_row_count(dev_cookie) == 1, "session must exist pre-logout"

    body_read_started = asyncio.Event()
    release_body_read = asyncio.Event()
    original_json_body = admin_users_api._json_body

    async def paused_json_body(req):
        if req.match_info.get("user_id") == victim_id:
            body_read_started.set()
            await release_body_read.wait()
        return await original_json_body(req)

    monkeypatch.setattr(admin_users_api, "_json_body", paused_json_body)

    async def _attack():
        return await client.patch(
            f"/agent-mcp/api/router/users/{victim_id}",
            data=json.dumps({"email": "raced-in@example.test"}),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": dev_cookie},
            allow_redirects=False,
        )

    attack_task = asyncio.ensure_future(_attack())
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    # Real concurrent logout, same session cookie, separate connection —
    # commits (deletes the session row) while the attack's body-read is
    # paused mid-flight.
    logout_resp = await logout_client.post(
        "/agent-mcp/logout",
        cookies={"agent_mcp_session": dev_cookie},
        allow_redirects=False,
    )
    assert logout_resp.status == 303, await logout_resp.text()
    assert _session_row_count(dev_cookie) == 0, (
        "session row must be genuinely deleted server-side before the "
        "paused request resumes"
    )

    release_body_read.set()
    resp = await asyncio.wait_for(attack_task, timeout=5)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"

    victim_row = _identity_module().get_user_by_username("victim-r9f4")
    assert victim_row["email"] != "raced-in@example.test", (
        "edit must NOT have landed using a session that was already "
        "logged out mid-flight"
    )


# ── Test B: happy-path regression — still-logged-in caller unaffected ─


async def test_non_racing_edit_user_still_succeeds(
    aiohttp_client, router_app,
) -> None:
    """Regression: a normal (non-racing), still-logged-in caller must
    not be spuriously rejected by the new session-liveness re-check."""
    # NOTE: the sentinel operator the default ``aiohttp_client`` fixture
    # logs in as is only auto-bootstrapped by ``init_router_db`` while
    # the users table is still completely empty (Phase 1 PR C's
    # first-run bootstrap invariant) — so the client must be created
    # (and its login round-trip completed) BEFORE any other user gets
    # seeded into a fresh DB, or the sentinel bootstrap is skipped and
    # the auto-login 401s.
    client = await aiohttp_client(router_app)
    victim_id = _seed_user("victim-r9f4b", is_sysadmin=False)

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{victim_id}",
        data=json.dumps({"email": "still-valid@example.test"}),
        headers=_REST_HEADERS,
    )
    assert resp.status == 200, await resp.text()
    row = _identity_module().get_user_by_username("victim-r9f4b")
    assert row["email"] == "still-valid@example.test"
