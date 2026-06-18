"""Operator-session auth middleware for the router (Phase 1 PR D).

PR C shipped the login + setup-wizard routes that create a session
cookie. PR D closes the dashboard surface: every ``/agent-mcp/...``
request that isn't an explicit unauth allow-list path must carry a
valid ``agent_mcp_session`` cookie OR a legacy ``Authorization:
Bearer <admin_token>`` header (for agent-side traffic on ``/mcp/``).

The legacy bearer path stays usable on ``/agent-mcp/mcp/...`` so
spawned agents keep authenticating; it is NOT honoured on the
dashboard's ``/api/...`` surface — the dashboard moves fully to
cookies. ADR 0014 retired the ``/agent-mcp/__*`` shape entirely.

Project-scoped paths (``/agent-mcp/api/<project>/...`` and
``/agent-mcp/app/<project>/...``) additionally verify the resolved
operator has a row in ``project_membership`` for that project.

Why a single middleware (rather than per-route deps the way FastAPI
sets it up): the router is aiohttp, and aiohttp's idiomatic
auth-gating pattern is a middleware that walks the path against an
allow-list. Per-route ``Depends``-style injection isn't in aiohttp's
vocabulary; doing it manually on every handler bloats the call sites
and risks drift.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Awaitable, Callable

from aiohttp import web

from .login import resolve_current_user


logger = logging.getLogger(__name__)


# Phase 3 Wave 2 (v5.0.69): HTTP methods that are treated as
# mutations for the per-project operator/viewer split. Anything
# OUTSIDE this set is a read (operator OR viewer admits). Anything
# inside requires the operator tier — viewers get 403. GET / HEAD /
# OPTIONS are reads; POST / PATCH / DELETE / PUT are mutations. The
# distinction is deliberately HTTP-verb-shaped (not "is this URL a
# mutation URL") so a future read-only POST (rare; e.g. search) would
# need an explicit allow-list rather than silently flipping a viewer
# into write access.
_MUTATION_METHODS = frozenset({"POST", "PATCH", "DELETE", "PUT"})


# ── Path policy ────────────────────────────────────────────────────


# Prefixes that bypass operator-session gating entirely. Every entry
# here is INTENTIONAL — adding a new one should come with a written
# justification in the PR body.
#
#   * ``/agent-mcp/login`` + ``/agent-mcp/logout``: the auth handshake
#     itself MUST be reachable without a cookie, or an operator who
#     has logged out has no way back in.
#   * ``/agent-mcp/setup``: the first-boot wizard runs before any
#     user exists (PR C); the empty-users middleware bounces
#     dashboard traffic to it.
#   * ``/agent-mcp/assets/``: Next.js static bundle. Public by design.
#   * ``/agent-mcp/mcp/``: MCP transport. Agent-side bearer auth lives
#     in ``backend_mcp_handler``; cookies don't apply.
#   * ``/agent-mcp/api/router/health``: public service descriptor
#     (ADR 0014). External monitors probe liveness without minting
#     an operator session. The path is exact-prefixed because every
#     other ``/api/router/...`` route falls into the session gate.
_UNAUTH_PREFIXES = (
    "/agent-mcp/login",
    "/agent-mcp/logout",
    "/agent-mcp/setup",
    "/agent-mcp/assets/",
    "/agent-mcp/mcp/",
    "/agent-mcp/api/router/health",
)


# Exact paths that bypass auth — service descriptor + the bare
# /agent-mcp landing redirect. We don't list `/agent-mcp/` itself
# here because we DO want the redirect handler to fire (which
# 303s to /login or the dashboard depending on auth state).
_UNAUTH_EXACT = frozenset({
    # The HEAD `/agent-mcp` (no trailing slash) is the 301 to
    # `/agent-mcp/`; let it through so we don't double-301.
    "/agent-mcp",
})


# Project-scoped prefixes that REQUIRE project_membership for the
# resolved operator. Captured as (regex, group_name) so the project
# slug can be extracted in O(1) match.
_PROJECT_SCOPED_PATTERNS = (
    re.compile(r"^/agent-mcp/api/(?P<project>[^/]+)(?:/|$)"),
    re.compile(r"^/agent-mcp/app/(?P<project>[^/]+)(?:/|$)"),
)


# Project-segment values under /api/ that are NOT projects (they're
# router-level admin endpoints). Membership-check skipped for these;
# the global operator-session gate still applies. ADR 0014: the
# single ``router`` segment replaces the prior per-route ``projects``
# entry — every admin endpoint lives at ``/api/router/...`` so this
# is the only top-level segment we have to exempt.
_NON_PROJECT_API_SEGMENTS = frozenset({"router"})


# ── Helpers ────────────────────────────────────────────────────────


def _single_tenant_mode() -> bool:
    """Return True iff the router is running in single-tenant mode.

    Lazy import so this module stays free of app-level import-time
    side effects. Single-tenant deploys (ADR-0008) are pinned to one
    operator-owned host; Phase 1 of the operator-login plan does not
    gate that audience — Phase 3 will revisit when groups + system
    perms arrive.
    """
    try:
        from . import app as _app
        return _app.SINGLE_TENANT_NAME is not None
    except Exception:  # pragma: no cover - defensive
        return False


def _path_is_unauth(path: str) -> bool:
    """Return True iff ``path`` skips the operator-session gate."""
    if path in _UNAUTH_EXACT:
        return True
    return any(path.startswith(p) for p in _UNAUTH_PREFIXES)


def _project_exists(project_name: str) -> bool:
    """Return True iff ``project_name`` is registered in the project
    registry (or is a known alias).

    Membership is only meaningful for projects the router actually
    serves. A request to ``/agent-mcp/app/typo-project/`` from a
    logged-in operator should fall through to the handler — which
    will emit 404 — rather than be rejected with a 401 that
    discloses "this project exists but you can't see it".

    The import is lazy so this module stays free of router.app
    import-time circular hazards. Failures are treated as "project
    does not exist" so a deploy with no registry file still works.
    """
    try:
        from .app import _projects_dict
        from .project_registry import ProjectRegistry  # noqa: F401
    except Exception:  # pragma: no cover - defensive
        return False
    try:
        projects = _projects_dict()
    except Exception:  # pragma: no cover - defensive
        return False
    if project_name in projects:
        return True
    # Aliased projects appear in the alias map; treat them as
    # existing so the alias's project_membership check fires against
    # the resolved (real) project. The current handler resolves
    # aliases itself, but the operator-level gate is "can you see
    # this URL at all?" — aliases count for that.
    try:
        from .app import _resolve_project_or_alias
        real_name, alias_entry = _resolve_project_or_alias(project_name)
        return alias_entry is not None or real_name in projects
    except Exception:  # pragma: no cover - defensive
        return False


def _project_from_path(path: str) -> str | None:
    """Return the project slug if ``path`` is project-scoped, else None.

    Filters out ``/api/projects`` (which is the project-lifecycle
    REST collection, not a project member-checked route).
    """
    for pat in _PROJECT_SCOPED_PATTERNS:
        m = pat.match(path)
        if m is None:
            continue
        project = m.group("project")
        if project in _NON_PROJECT_API_SEGMENTS:
            return None
        return project
    return None


def _unauth_response(message: str = "login_required") -> web.Response:
    """Render the JSON envelope dashboards expect on a 401.

    The dashboard's ``ApiClient`` (PR D in api.ts) keys off the
    ``error: "login_required"`` discriminator to redirect to
    ``/agent-mcp/login``; we keep the body small and machine-readable.
    """
    return web.json_response(
        {
            "error": "login_required",
            "message": message,
            "login_url": "/agent-mcp/login",
        },
        status=401,
    )


# ── Middleware ─────────────────────────────────────────────────────


@web.middleware
async def require_operator_session_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Enforce an operator session on every dashboard mutation/read.

    Decision matrix (top-to-bottom, first match wins):

      1. Non-``/agent-mcp/...`` paths: pass through. The router only
         owns the ``/agent-mcp`` mount; anything else (rare) is the
         test runner or a misconfiguration.
      2. Allow-listed prefixes (login/logout/setup/assets/mcp/...):
         pass through.
      3. No session cookie or invalid cookie: 401.
      4. Project-scoped path AND user is not a member of the project:
         401.
      5. Otherwise: stash ``request['user']`` for downstream handlers
         + dispatch to the handler.

    Side effect: ``identity.get_session`` slides ``last_used_at`` on
    a successful cookie resolution, keeping active sessions alive
    indefinitely as long as the operator is using the dashboard.
    """
    path = request.path
    if not path.startswith("/agent-mcp"):
        return await handler(request)
    if _path_is_unauth(path):
        return await handler(request)
    # Single-tenant mode: the deploy is pinned to one operator-owned
    # box (per ADR-0008); there is no multi-operator audience to gate
    # against. Phase 1 bypass — Phase 3 revisits when groups + system
    # perms arrive. Skipping the gate also lets the existing 410
    # "endpoint disabled in single-tenant mode" responses surface for
    # __create/__unregister/__rename (covered by nix/tests/single-tenant.nix).
    if _single_tenant_mode():
        return await handler(request)

    user = resolve_current_user(request)
    if user is None:
        return _unauth_response("session cookie missing or invalid")

    # Phase 3 Wave 2: sysadmin bypasses the project-membership check
    # entirely. The bit travels via either ``users.is_sysadmin = 1``
    # or membership in a group that's flagged sysadmin (the resolver
    # walks the transitive closure). Resolve once per request and
    # stash for downstream handlers.
    is_sysadmin = False
    try:
        from . import group_resolver
        is_sysadmin = group_resolver.resolve_user_is_sysadmin(
            user["user_id"]
        )
    except sqlite3.OperationalError:
        # router.db not migrated — same fail-closed UX as the
        # is_project_member path.
        is_sysadmin = False
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "sysadmin resolution failed for user %r; treating as non-sysadmin",
            user.get("username"),
        )
        is_sysadmin = False

    project = _project_from_path(path)
    if project is not None and _project_exists(project):
        if not is_sysadmin:
            # Phase 3 Wave 2: per-project role gating. Reads (GET /
            # HEAD / OPTIONS) admit on either tier; mutations
            # (POST / PATCH / PUT / DELETE) require operator —
            # viewer gets 403. The resolver walks group membership
            # too, so a viewer via a parent group is still a
            # viewer here.
            try:
                role = group_resolver.resolve_user_project_role(
                    user["user_id"], project
                )
            except sqlite3.OperationalError:
                role = None
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "project-role resolution failed for user=%r project=%r",
                    user.get("username"), project,
                )
                role = None
            if role is None:
                return _unauth_response(
                    f"operator {user['username']!r} has no membership in "
                    f"project {project!r}"
                )
            method = request.method.upper()
            if method in _MUTATION_METHODS and role != "operator":
                return web.json_response(
                    {
                        "error": "forbidden",
                        "message": (
                            f"viewer-tier operator {user['username']!r} "
                            f"cannot mutate project {project!r}"
                        ),
                    },
                    status=403,
                )

    # Stash the resolved user (and sysadmin flag) on the request so
    # downstream handlers (audit logging, sysadmin-only routes, future
    # per-route policy) can use them without re-resolving.
    request["user"] = user
    request["is_sysadmin"] = is_sysadmin
    return await handler(request)


__all__ = [
    "require_operator_session_middleware",
]
