"""Operator-session auth middleware for the router (Phase 1 PR D).

PR C shipped the login + setup-wizard routes that create a session
cookie. PR D closes the dashboard surface: every ``/agent-mcp/...``
request that isn't an explicit unauth allow-list path must carry a
valid ``agent_mcp_session`` cookie OR an ``Authorization: Bearer
<agent_token>`` header (for agent-side traffic on ``/mcp/``).

The bearer path stays usable on ``/agent-mcp/mcp/...`` so spawned
agents keep authenticating with their per-agent token; it is NOT
honoured on the dashboard's ``/api/...`` surface — the dashboard
moves fully to cookies. ADR 0014 retired the ``/agent-mcp/__*`` shape
entirely. retire-system-token Wave 1 (PR #208) removed the
``admin_token`` god-key bearer; only per-agent tokens are accepted.

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
from urllib.parse import quote

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
    # Phase 3 Wave 3 (prancy-napping-pie): the SSO handshake itself
    # MUST be reachable without a session cookie, or an operator
    # mid-flow can't complete the redirect dance with the IdP.
    "/agent-mcp/sso/",
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


def _try_proxy_header_identity(
    request: web.Request,
) -> dict[str, object] | None:
    """Resolve a user from the trusted proxy header, if SSO mode allows.

    Returns None when:

      * proxy-header SSO mode isn't active,
      * the request didn't originate from a trusted source IP,
      * the trusted header is missing or empty,
      * the SSO config layer fails to load (defensive — surfaced via
        the journal; the legacy cookie path still functions).

    The trusted-source check inside ``sso.extract_proxy_header_user``
    is what prevents a remote attacker from spoofing the header to
    impersonate an operator. We deliberately do NOT consult
    ``X-Forwarded-For`` here — that header is operator-supplied and
    the IP check is the trust gatekeeper.
    """
    try:
        from . import sso

        settings = sso.get_sso_config()
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "SSO config load failed in auth middleware; falling "
            "through to session-cookie-only path.",
        )
        return None
    if settings.mode is not sso.SSOMode.PROXY_HEADER or settings.proxy is None:
        return None
    try:
        return sso.extract_proxy_header_user(request, settings.proxy)
    except sqlite3.OperationalError:
        # Router DB not yet migrated — same fail-closed UX as the
        # session-cookie path.
        return None
    except Exception:  # pragma: no cover - defensive
        logger.exception("proxy-header SSO lookup failed; rejecting")
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


def _wants_html(request: web.Request) -> bool:
    """Return True iff the caller is a browser asking for HTML.

    The bug this guards against: a browser user with no session who
    visits ``/agent-mcp/`` was getting raw JSON splattered into the
    viewport (``{"error": "login_required", ...}``) and reading it
    as a system error. Browsers don't auto-follow a ``login_url``
    field in a JSON body — only fetch-based clients do — so we have
    to emit an HTTP-level redirect for them.

    Detection is Accept-header-only and deliberately conservative:

      * ``text/html`` must appear in ``Accept``.
      * The FIRST media type in the list must not be a JSON type.
        Browsers send ``Accept: text/html,...`` (HTML first); API
        clients send ``Accept: application/json...`` or
        ``Accept: application/vnd.agent-mcp.v1+json`` (JSON first).
      * No Accept header at all → False (safe default for non-browser
        tooling like ``curl`` with no flags).

    Anything ambiguous (no Accept, ``*/*``, JSON-first) falls through
    to the existing 401 JSON behaviour so we don't break programmatic
    callers.
    """
    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        return False
    first = accept.split(",", 1)[0].strip().lower()
    # Both ``application/json`` and vendor-suffixed JSON
    # (``application/vnd.*+json``) are JSON to us.
    if "json" in first:
        return False
    return True


def _login_redirect_response(request: web.Request) -> web.Response:
    """Return a 303 to ``/agent-mcp/login`` that preserves the deep link.

    The original path + query is URL-encoded into ``?next=`` so the
    login handler's existing ``_safe_next`` validation (login.py:
    ``_safe_next``) bounces the operator back to where they started
    after a successful login. ``_safe_next`` already requires the
    target to live under ``/agent-mcp/``, so an attacker can't smuggle
    an external redirect in via the next parameter.

    Implementation note: we build a bare ``web.Response`` with an
    explicit ``Location`` header rather than ``web.HTTPSeeOther``.
    ``HTTPSeeOther`` (via yarl) re-parses the location, decoding
    ``%3F`` back to ``?`` because ``?`` is technically legal inside a
    query value — which then makes the login handler's
    ``request.rel_url.query.get("next")`` truncate at the first
    embedded ``?``, losing everything after it (e.g. ``?page=memories``
    on a deep-linked dashboard URL). Setting the header directly keeps
    the percent-encoded bytes verbatim across the wire.
    """
    # ``request.path_qs`` is the raw "path?query" string the client
    # sent; ``quote`` with default ``safe="/"`` percent-encodes the
    # ``?`` and ``=`` so the login form sees one opaque next-value.
    next_target = quote(request.path_qs)
    location = f"/agent-mcp/login?next={next_target}"
    return web.Response(status=303, headers={"Location": location})


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
        # Phase 3 Wave 3 (prancy-napping-pie): proxy-header trust
        # mode. When the operator has configured an upstream proxy
        # to populate ``AGENT_MCP_SSO_PROXY_HEADER``, ALSO accept a
        # session-equivalent identity derived from that header — but
        # ONLY if the request arrives from a configured trusted
        # source IP. Otherwise an attacker who reaches the router
        # directly could spoof the header and walk in. The trusted-
        # source enforcement lives inside
        # ``sso.extract_proxy_header_user``; this branch is purely
        # the wiring.
        user = _try_proxy_header_identity(request)
        if user is None:
            # Browser callers get an HTML 303 to the login form so
            # they don't see a raw JSON envelope splattered into the
            # viewport. API callers keep the 401 JSON contract (the
            # dashboard's ApiClient redirects on the
            # ``error: "login_required"`` discriminator).
            if _wants_html(request):
                return _login_redirect_response(request)
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

    # Wave 6 PR 0: build a Principal once, here, at the outermost
    # seam that has identity + project + sysadmin in hand. Downstream
    # tool calls thread the same Principal through every gate
    # instead of re-deriving "who is this caller?" from ContextVars.
    # Lazy import — Principal is a leaf module but importing here
    # avoids a top-level dependency between the router package and
    # the per-project core package.
    try:
        from ..core.capabilities import resolve_capabilities
        from ..core.principal import Principal

        principal_user_id = (
            str(user.get("user_id"))
            if user.get("user_id") is not None
            else None
        )
        principal_project_name = (
            project
            if project is not None and _project_exists(project)
            else None
        )
        principal_project_role = (
            None
            if is_sysadmin or project is None or not _project_exists(project)
            else _safe_resolve_role(user.get("user_id"), project)
        )
        # Wave 9 PR 0: capabilities resolved at the seam; threaded into
        # Principal once.
        principal_capabilities = resolve_capabilities(
            user_id=principal_user_id,
            agent_id=None,
            sysadmin=is_sysadmin,
            agent_role=None,
            project_role=principal_project_role,
            kind="operator_session",
        )
        request["principal"] = Principal(
            kind="operator_session",
            user_id=principal_user_id,
            agent_id=None,
            sysadmin=is_sysadmin,
            project_name=principal_project_name,
            project_role=principal_project_role,
            agent_role=None,
            can_wake_loop=False,
            source_token=None,
            capabilities=principal_capabilities,
        )
    except Exception:  # pragma: no cover - defensive
        # Principal stash is additive; if construction fails for any
        # reason the legacy ContextVar path still admits the request.
        # The bridge in dispatch_tool_call falls back to ContextVars
        # when ``request["principal"]`` is missing.
        logger.exception(
            "Principal construction failed for user=%r; falling back "
            "to ContextVar path",
            user.get("username"),
        )
    return await handler(request)


def _safe_resolve_role(user_id: object, project: str) -> str | None:
    """Best-effort ``resolve_user_project_role`` that never raises.

    Wave 6 PR 0 — used when building the operator-session Principal
    for non-sysadmin callers. Mirrors the resolver chain the
    primary gate already walked above (so we don't pay for a second
    DB round-trip on the happy path the resolver above already
    cached results from); failures collapse to ``None`` so the
    Principal still gets stashed and the bridge has something to
    consume.
    """
    if user_id is None:
        return None
    try:
        from . import group_resolver
        return group_resolver.resolve_user_project_role(str(user_id), project)
    except Exception:  # pragma: no cover - defensive
        return None


__all__ = [
    "require_operator_session_middleware",
]
