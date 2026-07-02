"""Per-IP rate limiting for auth-sensitive router endpoints.

Internet-hardening (H1 + H2): the login POST runs an argon2id verify
per request, so an unthrottled attacker gets both a brute-force
oracle and a CPU/memory DoS amplifier. These tests pin:

  * the sliding-window limiter's allow/deny math + pruning,
  * client-IP resolution honouring X-Forwarded-For ONLY from a
    trusted peer,
  * the middleware returning 429 + Retry-After on the login POST for
    an over-budget IP, while a different IP is unaffected.
"""

from __future__ import annotations

import pytest

from agent_mcp.router.rate_limit import (
    RateLimitConfig,
    SlidingWindowLimiter,
    resolve_client_ip,
)


# ── Unit: SlidingWindowLimiter ─────────────────────────────────────


def test_limiter_allows_up_to_max_then_blocks() -> None:
    lim = SlidingWindowLimiter(max_events=3, window_seconds=60)
    t = 1000.0
    assert lim.check("ip", now=t) == (True, 0.0)
    assert lim.check("ip", now=t) == (True, 0.0)
    assert lim.check("ip", now=t) == (True, 0.0)
    allowed, retry_after = lim.check("ip", now=t)
    assert allowed is False
    # oldest hit ages out at t+60, so retry_after ≈ 60.
    assert 59.0 <= retry_after <= 60.0


def test_limiter_is_per_key() -> None:
    lim = SlidingWindowLimiter(max_events=1, window_seconds=60)
    t = 1000.0
    assert lim.check("a", now=t)[0] is True
    assert lim.check("a", now=t)[0] is False
    # A different key has its own budget.
    assert lim.check("b", now=t)[0] is True


def test_limiter_recovers_after_window() -> None:
    lim = SlidingWindowLimiter(max_events=1, window_seconds=60)
    assert lim.check("ip", now=1000.0)[0] is True
    assert lim.check("ip", now=1000.0)[0] is False
    # After the window elapses the budget is fresh again.
    assert lim.check("ip", now=1061.0)[0] is True


def test_limiter_prunes_idle_keys_over_time() -> None:
    lim = SlidingWindowLimiter(max_events=5, window_seconds=60)
    for i in range(50):
        lim.check(f"ip-{i}", now=1000.0)
    assert len(lim) == 50
    # Well past the window: every hit has aged out, so prune drops the
    # keys entirely rather than leaking them forever.
    lim.prune(now=2000.0)
    assert len(lim) == 0


def test_limiter_zero_max_is_unlimited() -> None:
    lim = SlidingWindowLimiter(max_events=0, window_seconds=60)
    for _ in range(100):
        assert lim.check("ip", now=1000.0) == (True, 0.0)


# ── Unit: client-IP resolution ─────────────────────────────────────


def _mocked(remote: str, xff: str | None = None):
    from unittest import mock

    from aiohttp.test_utils import make_mocked_request

    headers = {"X-Forwarded-For": xff} if xff else {}
    transport = mock.Mock()
    transport.get_extra_info = lambda key, default=None: (
        (remote, 40000) if key == "peername" else default
    )
    return make_mocked_request(
        "POST", "/agent-mcp/login", headers=headers, transport=transport,
    )


def test_client_ip_honours_xff_from_trusted_peer() -> None:
    cfg = RateLimitConfig.from_env()
    req = _mocked("127.0.0.1", xff="9.9.9.9, 10.0.0.1")
    # Loopback peer is trusted by default → leftmost XFF is the client.
    assert resolve_client_ip(req, cfg) == "9.9.9.9"


def test_client_ip_ignores_xff_from_untrusted_peer() -> None:
    cfg = RateLimitConfig.from_env()
    req = _mocked("203.0.113.7", xff="9.9.9.9")
    # Untrusted direct peer → XFF is attacker-controlled, use the peer.
    assert resolve_client_ip(req, cfg) == "203.0.113.7"


# ── Integration: middleware 429 on the login POST ──────────────────


@pytest.fixture
def rl_app(router_module, monkeypatch):
    """A router app with a low auth rate-limit for fast assertions."""
    monkeypatch.setenv("AGENT_MCP_RATELIMIT_AUTH_MAX", "3")
    monkeypatch.setenv("AGENT_MCP_RATELIMIT_AUTH_WINDOW", "60")
    return router_module.make_app()


def _seed_user(username: str = "rluser", password: str = "pw") -> None:
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    identity.create_user(username=username, password=password)


@pytest.mark.asyncio
@pytest.mark.no_auth_seed_session
async def test_login_post_rate_limited_returns_429(
    aiohttp_client, rl_app,
) -> None:
    _seed_user()
    client = await aiohttp_client(rl_app)
    xff = {"X-Forwarded-For": "1.2.3.4"}

    # First 3 attempts spend the budget (wrong creds → 401 each).
    for _ in range(3):
        resp = await client.post(
            "/agent-mcp/login",
            data={"username": "rluser", "password": "wrong"},
            headers=xff,
            allow_redirects=False,
        )
        assert resp.status == 401, await resp.text()

    # 4th attempt from the same IP → throttled.
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "rluser", "password": "wrong"},
        headers=xff,
        allow_redirects=False,
    )
    assert resp.status == 429, await resp.text()
    assert resp.headers.get("Retry-After")
    assert int(resp.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
@pytest.mark.no_auth_seed_session
async def test_rate_limit_is_per_ip(aiohttp_client, rl_app) -> None:
    _seed_user()
    client = await aiohttp_client(rl_app)

    # Exhaust the budget for one IP.
    for _ in range(4):
        await client.post(
            "/agent-mcp/login",
            data={"username": "rluser", "password": "wrong"},
            headers={"X-Forwarded-For": "1.2.3.4"},
            allow_redirects=False,
        )

    # A different client IP is unaffected.
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "rluser", "password": "wrong"},
        headers={"X-Forwarded-For": "5.6.7.8"},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


@pytest.mark.asyncio
@pytest.mark.no_auth_seed_session
async def test_rate_limit_disabled_via_env(
    aiohttp_client, router_module, monkeypatch,
) -> None:
    """AGENT_MCP_RATELIMIT_DISABLED=1 → no throttling."""
    monkeypatch.setenv("AGENT_MCP_RATELIMIT_DISABLED", "1")
    monkeypatch.setenv("AGENT_MCP_RATELIMIT_AUTH_MAX", "2")
    app = router_module.make_app()
    _seed_user()
    client = await aiohttp_client(app)
    for _ in range(6):
        resp = await client.post(
            "/agent-mcp/login",
            data={"username": "rluser", "password": "wrong"},
            headers={"X-Forwarded-For": "1.2.3.4"},
            allow_redirects=False,
        )
        assert resp.status == 401, await resp.text()
