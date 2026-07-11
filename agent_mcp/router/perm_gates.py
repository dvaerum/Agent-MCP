"""Route-level permission gates for the router admin surface.

Phase 3 Wave 2 (v5.0.69) of prancy-napping-pie. Wave 1b shipped the
user / group / project-membership CRUD routes behind a permissive
"any logged-in operator" gate. Wave 2 shipped the per-route
``@require_sysadmin`` wrapper this module owned exclusively until
Wave 9 PR 4 replaced it.

Wave 9 PR 4 (prancy-napping-pie) superseded ``@require_sysadmin``
with :func:`require_capability` — a capability-shaped decorator
that consults the per-request :class:`Principal` built by
``require_operator_session_middleware`` (Wave 6 PR 0). Wave 9 PR 6
deleted ``require_sysadmin``; :func:`require_capability` is now the
only route-level gate this module exposes.

WHY a separate module from ``auth_middleware``? The middleware owns
the global "do we have a session at all?" gate; this module owns
per-handler authorisation. Splitting the two keeps the middleware
small (still a single decision matrix) and lets per-handler gates
get richer over time (Wave 3 OIDC group-claim mapping in particular
needed new helpers here; Wave 9 PR 4's capability gate is the next
step in that progression).
"""

from __future__ import annotations

import functools
from typing import Awaitable, Callable

from aiohttp import web

from .single_tenant import bypasses_operator_gate


__all__ = ["require_capability"]


def require_capability(
    cap: str,
) -> Callable[
    [Callable[[web.Request], Awaitable[web.StreamResponse]]],
    Callable[[web.Request], Awaitable[web.StreamResponse]],
]:
    """Reject the request with 403 unless the caller carries ``cap``.

    Wave 9 PR 4 of prancy-napping-pie. The capability-shaped gate for
    router-admin routes; Wave 9 PR 6 deleted the legacy
    ``require_sysadmin`` wrapper this function replaced.

    Reads the per-request :class:`agent_mcp.core.principal.Principal`
    that ``require_operator_session_middleware`` stashes at
    ``request['principal']`` (Wave 6 PR 0) and consults
    :meth:`Principal.has_capability` directly — no second resolution
    chain, no second DB round-trip. Sysadmins admit unconditionally
    via the ``SYSADMIN_WILDCARD`` short-circuit inside
    :meth:`Principal.has_capability`; non-sysadmins admit when their
    resolved capability set (project-role bundle ∪ group-capability
    grants) contains ``cap``.

    Single-tenant mode (ADR-0008) bypasses the gate so the deploy is
    pinned to one operator-owned host. The legacy 410 / validation
    responses for single-tenant-disabled routes surface in their
    natural place rather than being pre-empted by a 403.

    Returns a JSON error envelope (``success: False``,
    ``error: "forbidden"``, ``message`` naming the missing cap and
    the caller) on reject. The dashboard's ApiClient keys off the
    status code (403) plus the ``error`` discriminator.

    Fail-closed: when ``request['principal']`` is missing — which
    happens only if a route is mounted in front of a path that
    bypasses the auth middleware entirely (an unintentional unauth
    allow-list slip) — the wrapper rejects with 403. The middleware
    constructs the Principal in a defensive try/except so a real
    construction failure under load also fails closed here rather
    than admitting silently.
    """

    def decorator(
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:

        @functools.wraps(handler)
        async def wrapper(request: web.Request) -> web.StreamResponse:
            # Single-tenant mode (ADR-0008): the deploy is pinned to
            # one operator box; there's no audience to gate against
            # here.
            if bypasses_operator_gate():
                return await handler(request)

            principal = request.get("principal")
            if principal is None or not principal.has_capability(cap):
                user = request.get("user") or {}
                username = user.get("username", "<unknown>")
                return web.json_response(
                    {
                        "success": False,
                        "error": "forbidden",
                        "message": (
                            f"operator {username!r} lacks capability "
                            f"{cap!r}; this action requires it"
                        ),
                    },
                    status=403,
                    headers={"Cache-Control": "no-store"},
                )
            return await handler(request)

        return wrapper

    return decorator
