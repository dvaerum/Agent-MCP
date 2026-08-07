"""SEC AC-R17-1: SSO/passwordless login must not crash or enumerate.

An SSO-provisioned user (OIDC or proxy-header mode) is stored with
``password_hash = NULL`` (``sso._create_passwordless_user`` INSERTs
NULL; migration 0003 makes the column nullable). A form login POST
against such a username used to reach
``identity.verify_password(user["password_hash"], password)`` with a
``None`` hash, which raised ``AttributeError`` inside argon2 →
uncaught → HTTP 500.

That 500 is a username-enumeration oracle on two axes:

  * **status**: SSO username → 500, while a bad password / unknown
    user → 401.
  * **timing**: the crash skips argon2 entirely (~0.01 ms vs ~63 ms for
    a real/decoy verify) — a ~6000x gap that reveals SSO accounts even
    if the 500 body is masked upstream.

The fix routes a found-but-passwordless user through the same
decoy-verify branch as a missing user: identical 401 response + one
argon2 verify against the fixed decoy hash. These tests pin that
contract and guard the real-password regression paths.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.asyncio


def _identity_module():
    """Import identity with router.db migrations applied."""
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username: str = "alice", password: str = "hunter2") -> str:
    """Create a password-backed user, return its user_id."""
    identity = _identity_module()
    return identity.create_user(username=username, password=password)


def _seed_sso_user(username: str = "sso-alice") -> str:
    """Insert an SSO-provisioned user row with ``password_hash = NULL``.

    Mirrors ``sso._create_passwordless_user`` — the passwordless INSERT —
    without pulling in the whole OIDC/proxy-header machinery. This is the
    row shape the login POST must handle without crashing or leaking.
    """
    import secrets

    identity = _identity_module()
    user_id = secrets.token_hex(8)
    created_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    with identity._connect() as conn:
        conn.execute(
            """
            INSERT INTO users
                (user_id, username, email, password_hash, created_at,
                 last_login_at, is_sysadmin, sso_subject)
            VALUES (?, ?, ?, NULL, ?, NULL, 0, ?)
            """,
            (user_id, username, f"{username}@idp.example", created_at,
             f"sub-{user_id}"),
        )
    return user_id


@pytest.mark.no_auth_seed_session
async def test_sso_passwordless_login_returns_401_not_500(
    aiohttp_client, router_app,
) -> None:
    """A login POST against an SSO (NULL-hash) user must 401, never 500.

    RED before the fix: ``verify_password(None, pw)`` raises
    AttributeError inside argon2 → middleware 500. The 500 itself is the
    enumeration oracle.
    """
    _seed_sso_user(username="sso-victim")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "sso-victim", "password": "anything"},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


@pytest.mark.no_auth_seed_session
async def test_sso_login_response_byte_identical_to_unknown_user(
    aiohttp_client, router_app,
) -> None:
    """No status/body oracle: an SSO account is indistinguishable from a
    nonexistent username.

    The login form echoes the submitted username, so bodies for
    *different* usernames legitimately differ — but that reflection is
    not an oracle (the attacker chose the username). The real oracle is
    whether *the same* submitted username reveals, via status or body,
    that it names an SSO account. We POST one fixed username while it is
    an SSO row, then delete the row and POST the identical request. Both
    responses must be byte-identical 401s: same status, same body.
    """
    identity = _identity_module()
    # A second account keeps the users table non-empty after we delete
    # the probe row — otherwise the empty-users setup-wizard middleware
    # bounces the second POST with a 303 instead of the 401 we assert.
    _seed_user(username="keep-nonempty", password="x")
    _seed_sso_user(username="probe-user")
    client = await aiohttp_client(router_app)

    payload = {"username": "probe-user", "password": "guess"}

    # (a) "probe-user" exists as an SSO/passwordless account.
    r_sso = await client.post(
        "/agent-mcp/login", data=payload, allow_redirects=False,
    )
    body_sso = await r_sso.text()

    # (b) Same username, now nonexistent — delete the row and re-POST.
    with identity._connect() as conn:
        conn.execute("DELETE FROM users WHERE username = ?", ("probe-user",))
    r_missing = await client.post(
        "/agent-mcp/login", data=payload, allow_redirects=False,
    )
    body_missing = await r_missing.text()

    assert r_sso.status == 401, body_sso
    assert r_missing.status == 401, body_missing
    # Same submitted username → the only variable is account type, which
    # must not leak. Byte-for-byte identical.
    assert body_sso == body_missing, (
        "an SSO account must be byte-identical to a nonexistent username"
    )


@pytest.mark.no_auth_seed_session
async def test_sso_login_status_matches_wrong_password_path(
    aiohttp_client, router_app,
) -> None:
    """Status + error-copy parity between the SSO and wrong-password 401.

    Bodies differ only in the (attacker-supplied) echoed username; the
    status and the error message must match so neither axis fingerprints
    an SSO account against a password account.
    """
    _seed_user(username="pw-real", password="rightpw")
    _seed_sso_user(username="sso-user")
    client = await aiohttp_client(router_app)

    r_sso = await client.post(
        "/agent-mcp/login",
        data={"username": "sso-user", "password": "guess"},
        allow_redirects=False,
    )
    r_badpw = await client.post(
        "/agent-mcp/login",
        data={"username": "pw-real", "password": "guess"},
        allow_redirects=False,
    )
    assert r_sso.status == r_badpw.status == 401
    assert "Invalid username or password." in await r_sso.text()
    assert "Invalid username or password." in await r_badpw.text()


@pytest.mark.no_auth_seed_session
async def test_sso_login_runs_equal_argon2_work_no_timing_oracle(
    aiohttp_client, router_app, monkeypatch,
) -> None:
    """Timing parity: SSO login pays exactly one decoy argon2 verify.

    The crash used to skip argon2 entirely (~6000x faster than a real
    verify). Post-fix, the passwordless user must flow through the SAME
    decoy branch as a missing user: one ``verify_password`` call against
    the constant decoy hash — identical work to the wrong-password and
    unknown-user paths, so response timing carries no signal.
    """
    import agent_mcp.router.identity as identity
    from agent_mcp.router import login as login_views

    _seed_user(username="pw-real2", password="rightpw")
    _seed_sso_user(username="sso-timing")

    calls: list[tuple[str, str]] = []
    real_verify = identity.verify_password

    def _spy(hashed: str, password: str) -> bool:
        calls.append((hashed, password))
        return real_verify(hashed, password)

    monkeypatch.setattr(identity, "verify_password", _spy)
    # login imported verify_password via the module, so patching the
    # identity attribute is what the call site sees.
    client = await aiohttp_client(router_app)

    # Wrong-password baseline: one verify against the real stored hash.
    calls.clear()
    r_badpw = await client.post(
        "/agent-mcp/login",
        data={"username": "pw-real2", "password": "wrong"},
        allow_redirects=False,
    )
    assert r_badpw.status == 401, await r_badpw.text()
    assert len(calls) == 1, "wrong-password path runs argon2 verify once"

    # SSO/passwordless: one verify against the DECOY hash (equal work),
    # never against ``None``.
    calls.clear()
    r_sso = await client.post(
        "/agent-mcp/login",
        data={"username": "sso-timing", "password": "wrong"},
        allow_redirects=False,
    )
    assert r_sso.status == 401, await r_sso.text()
    assert len(calls) == 1, (
        "SSO/passwordless path must run a dummy argon2 verify to equalise "
        "timing (not skip it via a crash)"
    )
    assert calls[0][0] == login_views._DECOY_PASSWORD_HASH, (
        "passwordless verify must target the constant decoy hash, not the "
        "user's NULL stored hash"
    )


@pytest.mark.no_auth_seed_session
async def test_real_password_user_still_logs_in_and_rejects_wrong_pw(
    aiohttp_client, router_app,
) -> None:
    """Regression: the passwordless guard must not weaken real logins."""
    _seed_user(username="pw-good", password="correcthorse")
    client = await aiohttp_client(router_app)

    ok = await client.post(
        "/agent-mcp/login",
        data={"username": "pw-good", "password": "correcthorse"},
        allow_redirects=False,
    )
    assert ok.status == 303, await ok.text()
    assert ok.headers.get("Set-Cookie"), "correct login must set a cookie"

    bad = await client.post(
        "/agent-mcp/login",
        data={"username": "pw-good", "password": "nope"},
        allow_redirects=False,
    )
    assert bad.status == 401, await bad.text()
