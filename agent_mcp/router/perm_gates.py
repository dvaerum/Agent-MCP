"""Route-level permission gates for the router admin surface.

Phase 3 Wave 2 (v5.0.69) of prancy-napping-pie. Wave 1b shipped the
user / group / project-membership CRUD routes behind a permissive
"any logged-in operator" gate. Wave 2 shipped the per-route
``@require_sysadmin`` wrapper this module owned exclusively until
Wave 9 PR 4 replaced it.

Wave 9 PR 4 (prancy-napping-pie) supersedes ``@require_sysadmin``
with :func:`require_capability` — a capability-shaped decorator
that consults the per-request :class:`Principal` built by
``require_operator_session_middleware`` (Wave 6 PR 0). The router-
admin route file (``admin_api``, ``admin_users_api``,
``admin_sso_api``) migrated to ``require_capability`` in the same
PR; ``require_sysadmin`` is kept as a deprecated bridge for one
cycle so any caller missed by the sweep still works. Wave 9 PR 6
deletes ``require_sysadmin`` once the bridge is no longer reachable.

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


__all__ = ["require_capability", "require_sysadmin"]


def require_sysadmin(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """Reject the request with 403 unless the caller is a sysadmin.

    Relies on ``require_operator_session_middleware`` having already
    resolved the session cookie and stamped
    ``request['is_sysadmin']`` (Phase 3 Wave 2). If the flag is
    missing — which happens only for paths that bypass the middleware
    entirely (the unauth allow-list) — the wrapper fails closed: a
    sysadmin-gated handler MUST run behind the auth middleware.

    Returns the standard error envelope shape used by the
    ``admin_users_api`` module so the dashboard's ApiClient can
    discriminate this 403 from a generic "operation failed". Keeping
    the wire shape identical to the other handler errors means the
    UI doesn't need a special case for "you're an operator but not a
    sysadmin".

    Wave 9 PR 4 (prancy-napping-pie) DEPRECATED — use
    :func:`require_capability` with a ``system.*.manage`` cap
    instead. The function is kept for one PR cycle so any caller
    missed by the migration sweep still works; PR 6 deletes it.
    """

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        # Single-tenant mode (ADR-0008) pins the deploy to a single
        # operator-owned host and bypasses operator-session auth
        # entirely in ``require_operator_session_middleware``. In
        # that mode every caller IS the implicit sysadmin — fall
        # through to the handler unchanged so the legacy 410 /
        # validation responses for single-tenant-disabled routes
        # surface in their natural place rather than being
        # pre-empted by a 403.
        try:
            from . import app as _app
            if _app.SINGLE_TENANT_NAME is not None:
                return await handler(request)
        except Exception:  # pragma: no cover - defensive
            pass

        if not request.get("is_sysadmin"):
            user = request.get("user") or {}
            username = user.get("username", "<unknown>")
            return web.json_response(
                {
                    "success": False,
                    "error": "forbidden",
                    "message": (
                        f"operator {username!r} is not a sysadmin; "
                        "this action requires sysadmin privileges"
                    ),
                },
                status=403,
                headers={"Cache-Control": "no-store"},
            )
        return await handler(request)

    return wrapper


def require_capability(
    cap: str,
) -> Callable[
    [Callable[[web.Request], Awaitable[web.StreamResponse]]],
    Callable[[web.Request], Awaitable[web.StreamResponse]],
]:
    """Reject the request with 403 unless the caller carries ``cap``.

    Wave 9 PR 4 of prancy-napping-pie. The capability-shaped
    successor to :func:`require_sysadmin`. The two coexist during
    the Wave 9 migration window; PR 6 deletes ``require_sysadmin``
    once every router-admin route has moved to a
    ``require_capability`` grant.

    Reads the per-request :class:`agent_mcp.core.principal.Principal`
    that ``require_operator_session_middleware`` stashes at
    ``request['principal']`` (Wave 6 PR 0) and consults
    :meth:`Principal.has_capability` directly — no second resolution
    chain, no second DB round-trip. Sysadmins admit unconditionally
    via the ``SYSADMIN_WILDCARD`` short-circuit inside
    :meth:`Principal.has_capability`; non-sysadmins admit when their
    resolved capability set (project-role bundle ∪ group-capability
    grants) contains ``cap``.

    Mirrors the single-tenant fall-through of
    :func:`require_sysadmin` so a single-tenant deploy keeps
    behaving identically: the route handler runs unchanged because
    the auth middleware itself bypasses the gate in that mode.

    Returns the same JSON error envelope shape as
    :func:`require_sysadmin` (``success: False``, ``error:
    "forbidden"``, ``message`` naming the missing cap and the
    caller). The dashboard's ApiClient keys off the status code
    (403) plus the ``error`` discriminator — keeping the wire shape
    identical means the UI doesn't need to special-case the
    capability-vs-sysadmin discriminator. The exact message text
    differs (cap name vs "is not a sysadmin") so log-grep and UX
    debugging surfaces remain distinct.

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
            # Single-tenant mode (ADR-0008): same fall-through as
            # ``require_sysadmin``. See its docstring for the
            # justification — the deploy is pinned to one operator
            # box; there's no audience to gate against here.
            try:
                from . import app as _app
                if _app.SINGLE_TENANT_NAME is not None:
                    return await handler(request)
            except Exception:  # pragma: no cover - defensive
                pass

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
