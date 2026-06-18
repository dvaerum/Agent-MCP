"""Route-level permission gates for the router admin surface.

Phase 3 Wave 2 (v5.0.69) of prancy-napping-pie. Wave 1b shipped the
user / group / project-membership CRUD routes behind a permissive
"any logged-in operator" gate. This module ships the per-route
``@require_sysadmin`` wrapper that the Wave 2 overhaul applies to:

  * project create / delete (``/api/router/projects[/<name>]``),
  * user CRUD (``/api/router/users[/<id>]``),
  * group CRUD (``/api/router/groups[/<id>]`` and its
    ``.../members`` sub-resource).

The wrapper consults the ``request['is_sysadmin']`` flag stamped on
the request by ``require_operator_session_middleware`` (which in
turn resolves through ``group_resolver.resolve_user_is_sysadmin``
so the sysadmin bit transits through nested groups). A missing
flag is conservatively treated as "not a sysadmin" so any
hypothetical handler reached without the middleware's authn pass
fails closed.

WHY a separate module from ``auth_middleware``? The middleware owns
the global "do we have a session at all?" gate; this module owns
per-handler authorisation. Splitting the two keeps the middleware
small (still a single decision matrix) and lets per-handler gates
get richer over time (Wave 3 OIDC group-claim mapping in particular
will need new helpers here).
"""

from __future__ import annotations

import functools
from typing import Awaitable, Callable

from aiohttp import web


__all__ = ["require_sysadmin"]


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
