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
import sqlite3
from typing import Awaitable, Callable

from aiohttp import web

from .single_tenant import bypasses_operator_gate


__all__ = ["require_capability", "revalidate_capability_or_403"]


def _forbidden_response(req: web.Request, cap: str) -> web.Response:
    """Shared 403 envelope for both the entry gate and the revalidation
    re-check below — same shape, same discriminator, one definition."""
    user = req.get("user") or {}
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


async def revalidate_capability_or_403(
    req: web.Request, cap: str,
) -> web.Response | None:
    """Re-check ``cap`` against a FRESH DB read; refresh the cached Principal.

    R6-F2 (HIGH, live-exploited): ``require_operator_session_middleware``
    resolves the caller's Principal (sysadmin flag + capability set) ONCE
    at request entry, before any body-bearing admin handler's genuine
    yield point — ``await req.read()`` inside ``admin_users_api._json_body``.
    An attacker who controls body-delivery pacing (a slow-drip POST/PATCH)
    can hold that read open while a concurrent request revokes their
    privilege in the DB; the paused handler then resumes and completes its
    write against the PRE-revocation snapshot cached at entry —
    ``require_capability`` below and every ``_caller_is_sysadmin`` /
    ``_caps_caller_lacks`` / ``_membership_grant_denied`` read in
    ``admin_users_api`` all consult that same stale snapshot.

    Mirrors R5-F1's fix for the sibling bug class (one-shot-authenticated
    long-lived operation must re-validate before it matters — there, the
    ``/api/events`` SSE stream re-running its open-time gate before every
    dispatch; here, a body-bearing handler re-running its entry-time gate
    before its write). Call this immediately AFTER the handler's
    ``_json_body`` (or any other body-read await) returns and BEFORE
    anything else that trusts the caller's privilege.

    On success this OVERWRITES ``req['principal']`` and
    ``req['is_sysadmin']`` in place with the freshly-resolved values, so
    every downstream self-escalation guard in the same handler — which
    reads those exact request-scoped keys — sees the live post-
    revalidation state too, not the snapshot taken at entry.

    R9-F4 (HIGH, live-exploited): the checks above re-derive capability
    and group/sysadmin state fresh, but until this fix the caller's
    IDENTITY was still trusted from the entry-time ``req['user']``
    snapshot — a session logged out DURING the yield point was
    invisible to the revalidation, so a privileged write could
    complete using an already-logged-out session. This function now
    also re-runs ``login.resolve_current_user`` against the request's
    own session cookie (when one is present) and denies if the session
    no longer resolves, before ever re-deriving capability/group state.

    Single-tenant mode (ADR-0008) bypasses, mirroring
    :func:`require_capability`. Fail-closed: a missing session user, an
    invalidated session, or any resolution error along the way, denies
    with the same 403 shape the entry gate uses.
    """
    if bypasses_operator_gate():
        return None

    user = req.get("user")
    user_id = user.get("user_id") if user else None
    if not user_id:
        # Reaching a body-bearing admin handler at all implies the
        # middleware already resolved a user; a missing id here means
        # something upstream is broken. Fail closed rather than let a
        # capability check run against no identity.
        return _forbidden_response(req, cap)

    # R9-F4 (HIGH, live-exploited): everything above and below this
    # block re-derives capability/group/sysadmin state from a FRESH DB
    # read, but the caller's IDENTITY itself — ``user`` / ``user_id`` —
    # still comes from ``req.get("user")``, the value the middleware
    # cached ONCE at request entry, before this handler's own yield
    # point. A session invalidated (logged out) DURING that yield point
    # is invisible to every check below: they all faithfully re-confirm
    # that the (now-stale) ``user_id`` still has the capability, never
    # that the SESSION which authenticated it is still live. Re-run the
    # exact same live lookup the entry-time middleware gate uses
    # (``login.resolve_current_user``, backed by ``identity.get_session``)
    # against the request's OWN session cookie, and deny if it no longer
    # resolves. Scoped to requests that actually carry a session cookie —
    # proxy-header SSO identities (Phase 3 Wave 3) have no session row to
    # invalidate and are re-verified fresh on every request already, so
    # they're left untouched here.
    from .login import SESSION_COOKIE_NAME, resolve_current_user

    session_id = req.cookies.get(SESSION_COOKIE_NAME, "")
    if session_id:
        try:
            live_user = resolve_current_user(req)
        except Exception:  # pragma: no cover - defensive
            live_user = None
        if live_user is None:
            return _forbidden_response(req, cap)

    from . import group_resolver
    from ..core.principal_builder import build_operator_principal

    try:
        groups: set[str] | None = set(
            group_resolver.resolve_user_groups(user_id)
        )
    except sqlite3.OperationalError:
        groups = None
    except Exception:  # pragma: no cover - defensive
        groups = None

    try:
        sysadmin = group_resolver.resolve_user_is_sysadmin(
            user_id, groups=groups,
        )
    except sqlite3.OperationalError:
        sysadmin = False
    except Exception:  # pragma: no cover - defensive
        sysadmin = False

    # These 8 handlers all live under ``/agent-mcp/api/router/...``,
    # which ``auth_middleware._project_from_path`` treats as the
    # non-project ``router`` admin segment (never project-scoped) — the
    # ENTRY-time Principal built for this route family always carries
    # ``project_role=None`` too, so re-deriving it here with the same
    # ``None`` keeps this revalidation exactly equivalent to a fresh
    # run of the entry-time construction, just later in the request.
    principal = build_operator_principal(
        user_id=str(user_id),
        kind="operator_session",
        project_role=None,
        sysadmin=sysadmin,
        groups=groups,
    )
    req["principal"] = principal
    req["is_sysadmin"] = sysadmin

    if not principal.has_capability(cap):
        return _forbidden_response(req, cap)
    return None


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
                return _forbidden_response(request, cap)
            return await handler(request)

        return wrapper

    return decorator
