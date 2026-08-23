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
operator has a row in ``project_membership`` for the project the URL
names — resolved through the same ADR-0010 alias-aware resolver the
proxy uses, so a grace-window alias gates identically to the real name
(N3 Tier 2).

Every path-classification fact this middleware consults ("is this path
public?", "is this a delivery route?", "is this project-scoped?") comes
from ``path_policy``, the ONE home shared with ``setup_wizard`` and
``app.backend_api_handler`` — see that module's docstring.

Why a single middleware (rather than per-route deps the way FastAPI
sets it up): the router is aiohttp, and aiohttp's idiomatic
auth-gating pattern is a middleware that walks the path against an
allow-list. Per-route ``Depends``-style injection isn't in aiohttp's
vocabulary; doing it manually on every handler bloats the call sites
and risks drift.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Awaitable, Callable
from urllib.parse import quote

from aiohttp import web

from . import mount
from . import path_policy
from .login import resolve_current_user
from .single_tenant import bypasses_operator_gate


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
#
# N3 Tier 2: the literal tuples/regexes that used to live here moved to
# ``path_policy`` — the ONE home for "is this path public / a delivery
# route / project-scoped?", shared with ``setup_wizard`` (the
# fresh-install redirect gate) and ``app.backend_api_handler`` (the
# Accept-version gate). The names below are re-exports so existing
# call sites and tests keep resolving; the policy itself has exactly
# one definition. See ``path_policy``'s module docstring for why the
# auth-bypass and setup-redirect prefix sets stay two NAMED policies
# rather than one merged list.

_UNAUTH_PREFIXES = path_policy.UNAUTH_PREFIXES
_UNAUTH_EXACT = path_policy.UNAUTH_EXACT
_PROJECT_SCOPED_PATTERNS = path_policy.PROJECT_SCOPED_PATTERNS
_NON_PROJECT_API_SEGMENTS = path_policy.NON_PROJECT_API_SEGMENTS


# ── Helpers ────────────────────────────────────────────────────────


def _path_is_unauth(path: str, app: web.Application | None = None) -> bool:
    """Return True iff ``path`` skips the operator-session gate.

    ``app`` supplies the DERIVED half of the allowlist: the exact
    canonical paths of routes whose handler carries ``path_policy.
    public_route`` (today just the ADR-0014 service descriptor and the
    trailing-slash alias ``_add_admin_trailing_slash_aliases`` derives
    from it). Passing ``None`` yields prefix-only matching, which is
    strictly more restrictive — the safe direction for a caller with no
    Application in hand.
    """
    return path_policy.is_unauth_path(path, app)


def _path_is_delivery(path: str) -> bool:
    """Return True iff ``path`` is an ADR-0021 delivery route
    (``/agent-mcp/api/<project>/delivery/{stream,status}``), which the backend
    authenticates by agent bearer, so it skips the operator-session gate."""
    return path_policy.is_delivery_path(path)


def _resolved_project_from_path(path: str) -> tuple[str | None, str | None]:
    """Return ``(url_segment, real_project_name)`` for a project-scoped
    ``path``; ``(None, None)`` when the path isn't project-scoped.

    ``real_project_name`` is None when the router doesn't serve that
    project at all — membership is only meaningful for projects that
    exist, and a request to ``/agent-mcp/app/typo-project/`` from a
    logged-in operator should fall through to the handler (which emits
    its own 404) rather than be rejected with a 401 that discloses
    "this project exists but you can't see it".

    N3 Tier 2 (question 4): resolution goes through the SAME
    ``app._resolve_project_or_alias`` the proxy uses, so the auth layer
    and the proxy layer agree on which project a URL names. They used to
    disagree during an ADR-0010 rename-with-grace window: the proxy
    resolved ``/api/<old-alias>/...`` to the real project while this
    layer looked ``project_membership`` up against the raw alias
    segment, found nothing, and handed a genuine member the
    unknown-project response. The ``/mcp`` transport already resolved
    first and gated second (``_forwarding_header_from_cookie(req,
    real_project_name)``); this brings the REST/dashboard surface into
    line with it.

    The imports are lazy so this module stays free of router.app
    import-time circular hazards. Failures are treated as "project does
    not exist" so a deploy with no registry file still works.
    """
    segment = path_policy.project_segment_from_path(path)
    if segment is None:
        return None, None
    try:
        from .app import _projects_dict, _resolve_project_or_alias
    except Exception:  # pragma: no cover - defensive
        return segment, None
    try:
        projects = _projects_dict()
    except Exception:  # pragma: no cover - defensive
        return segment, None
    if segment in projects:
        return segment, segment
    # Not a real project name — it may still be a live ADR-0010 grace
    # alias. ``_resolve_project_or_alias`` raises HTTPNotFound when it
    # is neither; that (and any registry failure) means "no project".
    try:
        real_name, _alias_entry = _resolve_project_or_alias(segment)
    except Exception:
        return segment, None
    return segment, (real_name if real_name in projects else None)


def _project_from_path(path: str) -> str | None:
    """Return the raw project URL segment if ``path`` is project-scoped,
    else None. Reserved non-project segments (``router``) yield None.

    The SYNTACTIC half of "which project is this?" — callers that need
    the real (alias-resolved) project want
    ``_resolved_project_from_path``.
    """
    return path_policy.project_segment_from_path(path)


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


def _unauth_response(
    message: str = "login_required", request: web.Request | None = None,
) -> web.Response:
    """Render the JSON envelope dashboards expect on a 401.

    The dashboard's ``ApiClient`` (PR D in api.ts) keys off the
    ``error: "login_required"`` discriminator to redirect to the login
    page. ADR-0020: ``login_url`` is emitted at the caller's mount prefix
    (root for Traefik, /agent-mcp for the tailnet) when the request is
    available, so the root dashboard redirects to /login not
    /agent-mcp/login.
    """
    login_url = (
        mount.external_path(request, "/login")
        if request is not None else "/agent-mcp/login"
    )
    return web.json_response(
        {
            "error": "login_required",
            "message": message,
            "login_url": login_url,
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
    # ADR-0020: send the operator to the login page at THEIR mount prefix
    # (root for Traefik, /agent-mcp for the tailnet) so a bare-root visit
    # stays at the root rather than bouncing into /agent-mcp/login.
    location = f"{mount.external_path(request, '/login')}?next={next_target}"
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
    # ADR-0020: gate on the CANONICAL (/agent-mcp-form) path, not
    # request.path. A root-aliased request (Traefik mounted at the host
    # root) arrives as /api/... — canonicalising it back to
    # /agent-mcp/api/... makes it hit the SAME gate + unauth allow-list
    # as its tailnet twin. SECURITY: keying off request.path here would
    # let every root-aliased route skip the gate (path wouldn't start
    # with /agent-mcp) and serve unauthenticated.
    path = mount.canonical_path(request)
    if not path.startswith("/agent-mcp"):
        return await handler(request)
    if _path_is_unauth(path, request.app):
        return await handler(request)
    # ADR-0021 delivery routes are agent-bearer-authed at the backend (like
    # /mcp/), so they skip the operator-session gate here — otherwise an agent's
    # delivery stream/status is rejected with login_required and delivery can
    # never work. The backend's require_agent_bearer is the real gate.
    if _path_is_delivery(path):
        return await handler(request)
    # Single-tenant mode: the deploy is pinned to one operator-owned
    # box (per ADR-0008); there is no multi-operator audience to gate
    # against. Phase 1 bypass — Phase 3 revisits when groups + system
    # perms arrive. Skipping the gate also lets the existing 410
    # "endpoint disabled in single-tenant mode" responses surface for
    # __create/__unregister/__rename (covered by nix/tests/single-tenant.nix).
    if bypasses_operator_gate():
        # SC-R6-1: single-tenant is an authorized audience (ADR-0008,
        # one operator-owned host), so the ``/app/`` dashboard warm-
        # start side-effect is permitted here. The flag is read by
        # ``dashboard_handler`` — we can't key that off ``req["user"]``
        # because this bypass never stashes a user.
        request["_warm_authorized"] = True
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
            return _unauth_response(
                "session cookie missing or invalid", request,
            )

    # Phase 3 Wave 2: sysadmin bypasses the project-membership check
    # entirely. The bit travels via either ``users.is_sysadmin = 1``
    # or membership in a group that's flagged sysadmin (the resolver
    # walks the transitive closure). Resolve once per request and
    # stash for downstream handlers.
    #
    # arch-r4 #3 (ResolvedOperator): ``resolve_user_groups`` below is
    # the ONE group-membership-graph walk this request pays for.
    # Every downstream consumer that used to independently re-walk
    # the same graph for the same ``user_id`` — the sysadmin check,
    # the project-role gate, and the Principal's capability
    # resolution — now takes the resulting ``groups`` set as an
    # explicit parameter instead of re-deriving it (see
    # ``group_resolver.resolve_user_is_sysadmin``/
    # ``resolve_user_project_role`` and
    # ``core.capabilities.resolve_capabilities``, all of which treat
    # ``groups=None`` as "not supplied, self-resolve" so every OTHER
    # caller keeps its original single-call behaviour). If the walk
    # itself fails, ``groups`` stays ``None`` and every downstream
    # call falls back to its own independent resolution — the exact
    # per-call retry-on-failure behaviour this middleware had before.
    from . import group_resolver

    groups: set[str] | None = None
    try:
        groups = set(group_resolver.resolve_user_groups(user["user_id"]))
    except sqlite3.OperationalError:
        groups = None
    except Exception:  # pragma: no cover - defensive
        groups = None

    is_sysadmin = False
    try:
        is_sysadmin = group_resolver.resolve_user_is_sysadmin(
            user["user_id"], groups=groups
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

    # N3 Tier 2 (question 4): ONE alias-aware resolution per request.
    # ``url_segment`` is what the caller typed (used only for the
    # syntactic /app/ vs /api/ branch); ``project`` is the REAL project
    # name the proxy will serve, so membership, the mutation gate and
    # the Principal are all keyed off the same identity the backend
    # sees. This replaced three separate ``_project_exists`` calls that
    # each re-read the registry AND resolved aliases only for the
    # existence question, never for the membership lookup.
    url_segment, project = _resolved_project_from_path(path)
    role: str | None = None
    if project is not None:
        if not is_sysadmin:
            # Phase 3 Wave 2: per-project role gating. Reads (GET /
            # HEAD / OPTIONS) admit on either tier; mutations
            # (POST / PATCH / PUT / DELETE) require operator —
            # viewer gets 403. The resolver walks group membership
            # too, so a viewer via a parent group is still a
            # viewer here.
            try:
                role = group_resolver.resolve_user_project_role(
                    user["user_id"], project, groups=groups
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
                # SEC round 3 (PF-1): a project the operator has no
                # membership in MUST be indistinguishable from one the
                # router doesn't serve, or the status+body differential
                # is a cross-tenant project-existence oracle — the same
                # class SEC5 closed on ``/mcp``. The old name-reflecting
                # 401 here (against a 404 "unknown project" for a
                # nonexistent slug, and a 200 SPA shell on ``/app/``)
                # let ANY authenticated user brute-force other tenants'
                # slugs. Hand back EXACTLY what the downstream handler
                # emits for an unknown project on each surface; no
                # project-name reflection.
                if path.startswith("/agent-mcp/app/"):
                    # ``/app/``: ``dashboard_handler`` serves the static
                    # SPA shell for ANY segment (existent or not), so a
                    # nonexistent slug already answers 200. Fall through
                    # to it — the shell carries no project data (all of
                    # that is gated behind ``/api/``), so a non-member
                    # sees the same response as for a bogus slug across
                    # every method (a POST 405s the GET-only route
                    # either way). We deliberately skip the user /
                    # Principal stash below: a non-member gets nothing
                    # but the public shell.
                    return await handler(request)
                # ``/api/``: mirror ``backend_api_handler``'s
                # unknown-project response (406 Accept-version gate
                # first — returned; else 404 "unknown project" —
                # RAISED, so it renders identically to the handler's
                # own path).
                from .app import unknown_project_response
                return unknown_project_response(request)
            method = request.method.upper()
            if method in _MUTATION_METHODS and role != "operator":
                return web.json_response(
                    {
                        "error": "forbidden",
                        "message": (
                            f"viewer-tier operator {user['username']!r} "
                            f"cannot mutate project {url_segment!r}"
                        ),
                    },
                    status=403,
                )

    # Stash the resolved user (and sysadmin flag) on the request so
    # downstream handlers (audit logging, sysadmin-only routes, future
    # per-route policy) can use them without re-resolving.
    request["user"] = user
    request["is_sysadmin"] = is_sysadmin

    # SC-R6-1: reaching here means the caller is AUTHORIZED for this
    # path — a sysadmin, or a member with a sufficient role for the
    # project-scoped segment (the non-member ``/app/`` branch returned
    # the bare SPA shell above WITHOUT falling through to this stash).
    # ``dashboard_handler`` reads this flag before scheduling the
    # per-project backend warm-start, so an authenticated non-member
    # can no longer activate an arbitrary tenant's backend via a plain
    # ``GET /agent-mcp/app/<victim>/``. The ``/app/`` response itself
    # stays a uniform 200 shell either way (no project-existence
    # oracle); only the spawn side-effect is gated.
    request["_warm_authorized"] = True

    # Wave 6 PR 0: build a Principal once, here, at the outermost
    # seam that has identity + project + sysadmin in hand. Downstream
    # tool calls thread the same Principal through every gate
    # instead of re-deriving "who is this caller?" from ContextVars.
    # Lazy import — Principal is a leaf module but importing here
    # avoids a top-level dependency between the router package and
    # the per-project core package.
    try:
        from ..core.principal_builder import build_operator_principal

        principal_user_id = (
            str(user.get("user_id"))
            if user.get("user_id") is not None
            else None
        )
        # N3 Tier 2: ``project`` is already the alias-resolved REAL
        # project name (or None when the router doesn't serve it), so
        # the Principal is scoped to the same project the proxy routes
        # to. Previously an alias URL stamped the Principal with the
        # ALIAS, which no per-project capability grant is keyed on.
        principal_project_name = project
        # arch-r4 #3: ``role`` is the SAME value the mutation gate
        # above already resolved (or ``None`` if that block never
        # ran, in which case the conditions below are ``None`` too) —
        # no second ``resolve_user_project_role`` call. This used to
        # be ``_safe_resolve_role(user.get("user_id"), project)``, a
        # wrapper that re-ran the identical resolution because the
        # gate's result was never stashed; deleted along with this
        # call site.
        principal_project_role = (
            None if is_sysadmin or project is None else role
        )
        # arch-B: capabilities resolved once via the shared builder (Wave
        # 9 PR 0 resolved them at this seam; the builder is now the single
        # home for that + the Principal construction). arch-r4 #3: pass
        # the already-resolved ``groups`` through so the builder's
        # capability resolution doesn't re-walk group_membership a
        # fourth time for this request.
        request["principal"] = build_operator_principal(
            user_id=principal_user_id,
            kind="operator_session",
            project_role=principal_project_role,
            sysadmin=is_sysadmin,
            project_name=principal_project_name,
            source_token=None,
            groups=groups,
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


__all__ = [
    "require_operator_session_middleware",
]
