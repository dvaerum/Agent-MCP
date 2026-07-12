"""Per-IP rate limiting for the router's auth-sensitive surface.

Threat model: open-internet exposure. The login POST
(``login.login_post_handler``) runs an argon2id verify (64 MiB,
multi-threaded) on every request, so an unthrottled attacker gets
both a password brute-force oracle AND a CPU/memory DoS amplifier
for free. The SSO callback / login handshake share the shape.

This module is a dependency-free, in-process sliding-window
limiter. The router is a single aiohttp process with no Redis /
shared cache to piggyback on (confirmed: the only cross-process
state is SQLite + Unix sockets), so an in-process ``dict`` of
IP → recent-timestamps is the pragmatic, correct-for-one-process
choice. If the router is ever fanned out behind multiple workers
the limiter would need a shared store; a signpost comment marks
that assumption.

Two limiters wire in via ``attach``:

  * ``auth`` — strict; guards the login POST, the SSO login start,
    and the SSO callback. Every hit (success OR failure) spends
    budget, so the argon2 DoS is capped before the verify runs.
  * ``global`` — loose, defence-in-depth for the rest of the
    surface. Disabled by default (max=0) so tailnet/dev traffic
    isn't hampered; operators opt in for public exposure.

Client-IP resolution honours ``X-Forwarded-For`` ONLY when the
direct peer is a trusted proxy — reusing the same trust posture as
``sso.is_trusted_proxy_source`` (the forwarded chain is
attacker-controllable, so the peer-IP check is the gatekeeper). The
chain is walked right-to-left past trusted-proxy hops to the first
untrusted client, never the spoofable leftmost entry (see
``resolve_client_ip``).
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Deque, Dict

from aiohttp import web


logger = logging.getLogger(__name__)


# ── Keys stashed on the aiohttp Application ────────────────────────


_CONFIG_KEY = "rate_limit_config"
_AUTH_LIMITER_KEY = "rate_limit_auth"
_GLOBAL_LIMITER_KEY = "rate_limit_global"


# Loopback is the default trusted proxy source: production deploys
# terminate TLS at nginx / tailscale and forward to the router over a
# Unix socket or loopback, so the direct peer of a forwarded request
# is 127.0.0.1 / ::1 (a UDS peer reports an empty ``request.remote``).
_DEFAULT_TRUSTED_PROXIES = "127.0.0.1,::1"


# ── Config ─────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int env var; fall back on garbage."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        logger.warning(
            "%s=%r is not an integer; using default %d", name, raw, default
        )
        return default
    return value if value >= 0 else default


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_trusted_proxies(raw: str | None) -> frozenset[str]:
    """Comma-separated IP list → canonical frozenset (garbage dropped)."""
    if not raw:
        return frozenset()
    out: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(str(ipaddress.ip_address(part)))
        except ValueError:
            logger.warning(
                "AGENT_MCP_RATELIMIT_TRUSTED_PROXIES: %r is not a valid "
                "IP; dropping.", part,
            )
    return frozenset(out)


@dataclass(frozen=True)
class RateLimitConfig:
    """Resolved limiter settings, read once from env at ``attach``."""

    enabled: bool
    auth_max: int
    auth_window: int
    global_max: int
    global_window: int
    trusted_proxies: frozenset[str]

    @classmethod
    def from_env(cls) -> "RateLimitConfig":
        # Default ON: the auth limiter is the protective baseline. The
        # global limiter defaults OFF (max=0) so dev/tailnet isn't
        # hampered; operators enable it for public exposure.
        enabled = not _env_truthy(
            os.environ.get("AGENT_MCP_RATELIMIT_DISABLED")
        )
        return cls(
            enabled=enabled,
            auth_max=_env_int("AGENT_MCP_RATELIMIT_AUTH_MAX", 10),
            auth_window=_env_int("AGENT_MCP_RATELIMIT_AUTH_WINDOW", 60),
            global_max=_env_int("AGENT_MCP_RATELIMIT_GLOBAL_MAX", 0),
            global_window=_env_int("AGENT_MCP_RATELIMIT_GLOBAL_WINDOW", 60),
            trusted_proxies=_parse_trusted_proxies(
                os.environ.get(
                    "AGENT_MCP_RATELIMIT_TRUSTED_PROXIES",
                    _DEFAULT_TRUSTED_PROXIES,
                )
            ),
        )


# ── The limiter ────────────────────────────────────────────────────


class SlidingWindowLimiter:
    """In-process per-key sliding-window counter.

    ``check(key)`` records a hit and returns ``(allowed, retry_after)``.
    When ``max_events`` hits already fall inside the trailing
    ``window_seconds``, the call is rejected (no hit recorded) and
    ``retry_after`` is the seconds until the oldest in-window hit ages
    out. ``prune`` drops empty deques so a scan of many one-shot IPs
    doesn't leak memory forever.

    Not thread-safe: the router runs a single asyncio event loop, and
    ``check`` never awaits, so all mutation is serialised by the loop.
    A multi-worker deployment would need a shared store instead.
    """

    def __init__(self, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = {}
        self._checks_since_prune = 0

    def check(
        self, key: str, *, now: float | None = None
    ) -> tuple[bool, float]:
        """Record a hit for ``key``; return ``(allowed, retry_after)``."""
        if self.max_events <= 0:
            return True, 0.0
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_seconds
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
        while dq and dq[0] <= cutoff:
            dq.popleft()
        # Opportunistic global prune so idle keys don't accumulate.
        self._checks_since_prune += 1
        if self._checks_since_prune >= 256:
            self.prune(now=now)
        if len(dq) >= self.max_events:
            retry_after = dq[0] + self.window_seconds - now
            return False, max(retry_after, 0.0)
        dq.append(now)
        return True, 0.0

    def prune(self, *, now: float | None = None) -> None:
        """Drop aged-out hits and any key whose deque emptied."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_seconds
        self._checks_since_prune = 0
        empty: list[str] = []
        for key, dq in self._hits.items():
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                empty.append(key)
        for key in empty:
            del self._hits[key]

    def __len__(self) -> int:  # tracked-key count, for tests + metrics
        return len(self._hits)


# ── Client-IP resolution ───────────────────────────────────────────


def _is_trusted_ip(ip: str, cfg: RateLimitConfig) -> bool:
    """Return True iff ``ip`` (a raw string) is a configured trusted proxy.

    Canonicalises the address first, then checks the limiter's own
    ``trusted_proxies`` config unioned with the SSO proxy-header
    trusted-IP set (so an operator who configured proxy-header SSO
    doesn't have to re-declare the same proxy IPs for the limiter).
    A non-parseable value is never trusted.
    """
    try:
        canonical = str(ipaddress.ip_address(ip))
    except ValueError:
        return False
    if canonical in cfg.trusted_proxies:
        return True
    try:
        from . import sso

        settings = sso.get_sso_config()
        if settings.proxy is not None:
            return canonical in settings.proxy.trusted_ips
    except Exception:  # pragma: no cover - defensive
        pass
    return False


def _is_trusted_proxy(request: web.Request, cfg: RateLimitConfig) -> bool:
    """Return True iff the direct peer may set ``X-Forwarded-For``.

    A UDS peer reports an empty ``request.remote`` and is treated as
    trusted loopback (the reverse proxy forwards over that socket).
    Otherwise the peer IP is matched against the trusted-proxy set via
    ``_is_trusted_ip``.
    """
    peer = request.remote or ""
    if not peer:
        # UDS / in-process transport — the reverse proxy forwards here.
        return True
    return _is_trusted_ip(peer, cfg)


def request_from_trusted_proxy(request: web.Request) -> bool:
    """True iff the direct peer is a trusted proxy that may set
    ``X-Forwarded-*`` headers.

    Reuses the limiter's trusted-proxy determination — loopback + UDS
    trusted by default (the ``nginx-on-loopback`` / Unix-socket posture),
    unioned with ``AGENT_MCP_RATELIMIT_TRUSTED_PROXIES`` and the SSO
    proxy-header trusted-IP set. This is the SAME trust boundary that
    decides whether ``X-Forwarded-For`` may be honoured, so it is the
    correct gate for whether ``X-Forwarded-Host`` / ``X-Forwarded-Proto``
    may be trusted when deriving the router's own external origin
    (login same-origin check, SSO redirect_uri, cookie-Secure). Callers
    that trust these headers from an UNTRUSTED peer let a client forge
    the router's computed self-origin (OBS7).

    Reads config from env on each call (login/SSO POSTs are rare), the
    same lazy pattern ``_is_trusted_ip`` already uses for the SSO IPs.
    """
    return _is_trusted_proxy(request, RateLimitConfig.from_env())


def resolve_client_ip(request: web.Request, cfg: RateLimitConfig) -> str:
    """Best-effort client IP for rate-limit keying.

    Honours ``X-Forwarded-For`` ONLY when the direct peer is a trusted
    proxy; otherwise the peer IP. Falls back to a constant sentinel
    when neither is available so limiting still engages (fail-closed)
    rather than silently disabling.

    Security (finding: XFF spoof bypass): the header is appended
    left-to-right as a request traverses proxies, so the LEFTMOST entry
    is fully client-controlled and MUST NOT be used for keying — an
    attacker who rotates it would mint a fresh brute-force / argon2-DoS
    budget per request. Instead, walk the chain RIGHT-TO-LEFT (from the
    hop our trusted proxy appended) skipping trusted-proxy IPs; the
    first untrusted entry is the real client. If every entry is a
    trusted proxy, fall back to the leftmost. This is correct
    regardless of how the edge proxy is configured (it does not rely on
    the edge overwriting the header).
    """
    if _is_trusted_proxy(request, cfg):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            hops = [part.strip() for part in xff.split(",") if part.strip()]
            for hop in reversed(hops):
                if not _is_trusted_ip(hop, cfg):
                    return hop
            if hops:
                # All hops are trusted proxies — no untrusted client in
                # the chain; the leftmost is as close as we can get.
                return hops[0]
    return request.remote or "unknown"


# ── Path policy ────────────────────────────────────────────────────


def _is_auth_path(request: web.Request) -> bool:
    """Return True for the auth-sensitive, argon2/credential endpoints.

    Only the mutating verbs count: the login GET renders a form and
    is cheap, but the login POST runs argon2; the SSO callback mints
    a session; the SSO login start kicks off the IdP round-trip.
    """
    path = request.path
    method = request.method.upper()
    if path == "/agent-mcp/login" and method == "POST":
        return True
    if path == "/agent-mcp/setup" and method == "POST":
        return True
    if path.startswith("/agent-mcp/sso/"):
        # Both the login-start and the callback are credential-adjacent.
        return True
    return False


# ── Response ───────────────────────────────────────────────────────


def _too_many_requests(retry_after: float) -> web.Response:
    """429 with an integer ``Retry-After`` (seconds), rounded up."""
    seconds = max(1, math.ceil(retry_after))
    return web.json_response(
        {
            "error": "rate_limited",
            "message": (
                "too many requests from your address; retry after "
                f"{seconds}s"
            ),
        },
        status=429,
        headers={
            "Retry-After": str(seconds),
            "Cache-Control": "no-store",
        },
    )


# ── Middleware + wire-up ───────────────────────────────────────────


@web.middleware
async def rate_limit_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Reject over-budget callers with 429 before the handler runs.

    Reads the per-app limiter instances stashed by ``attach``. When
    limiting is disabled (or not attached) this is a pure pass-through.
    The auth limiter runs before the global one so an auth flood is
    charged against the strict budget, not the loose one.
    """
    cfg: RateLimitConfig | None = request.app.get(_CONFIG_KEY)
    if cfg is None or not cfg.enabled:
        return await handler(request)

    client_ip = resolve_client_ip(request, cfg)

    if _is_auth_path(request):
        auth: SlidingWindowLimiter | None = request.app.get(_AUTH_LIMITER_KEY)
        if auth is not None:
            allowed, retry_after = auth.check(client_ip)
            if not allowed:
                logger.warning(
                    "rate-limit: auth endpoint %s throttled for %s",
                    request.path, client_ip,
                )
                return _too_many_requests(retry_after)

    glob: SlidingWindowLimiter | None = request.app.get(_GLOBAL_LIMITER_KEY)
    if glob is not None and glob.max_events > 0:
        allowed, retry_after = glob.check(client_ip)
        if not allowed:
            return _too_many_requests(retry_after)

    return await handler(request)


def attach(app: web.Application) -> RateLimitConfig:
    """Load config from env + stash limiter instances on ``app``.

    Called from ``make_app`` after construction (the middleware itself
    is added to the constructor's ``middlewares=`` list). Returns the
    resolved config so the caller can log the effective limits.
    """
    cfg = RateLimitConfig.from_env()
    app[_CONFIG_KEY] = cfg
    app[_AUTH_LIMITER_KEY] = SlidingWindowLimiter(
        cfg.auth_max, cfg.auth_window
    )
    app[_GLOBAL_LIMITER_KEY] = SlidingWindowLimiter(
        cfg.global_max, cfg.global_window
    )
    return cfg


__all__ = [
    "RateLimitConfig",
    "SlidingWindowLimiter",
    "attach",
    "rate_limit_middleware",
    "resolve_client_ip",
]
