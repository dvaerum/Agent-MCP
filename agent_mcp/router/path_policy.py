"""One canonical home for the router's request-classification facts
(N3 Tier 2).

Three questions used to be answered TWICE, by different modules, from
independently-maintained literal tuples/regexes:

  1. **Is this path public?** ``auth_middleware._UNAUTH_PREFIXES`` and
     ``setup_wizard._REDIRECT_EXEMPT_PREFIXES``.
  2. **Is this a delivery route?** ``auth_middleware._DELIVERY_RE`` (a
     compiled regex over the canonical path) and ``router/app.py``'s
     ``rest in ("delivery/stream", "delivery/status")`` tuple over the
     backend-facing path tail.
  3. **Which project is this?** ``auth_middleware._project_from_path``
     (raw URL segment) and ``app.py::_resolve_project_or_alias`` (the
     ADR-0010 alias-aware resolver the proxy uses).

Every consumer now derives its answer from this module. Two design
notes, because "make them consistent" is NOT the same as "make them one
list":

**The two prefix tuples are two different questions, deliberately.**
``UNAUTH_PREFIXES`` answers "does this path skip the operator-session
gate?"; ``REDIRECT_EXEMPT_PREFIXES`` answers "does this path skip the
fresh-install bounce to the setup wizard?". Their differences are
load-bearing, not drift:

  * ``/agent-mcp/login`` + ``/agent-mcp/logout`` are auth-bypass ONLY.
    On a fresh install (empty ``users`` table) an operator who lands on
    the login form must be bounced to ``/setup`` — there is no account
    to log into yet.
  * ``/agent-mcp/api/`` is redirect-exempt ONLY. The whole
    machine-to-machine REST surface must never be 303'd to an HTML
    wizard, but it stays fully operator-session gated.

Collapsing them into one list would break one of those two. They share
a home and a matcher instead, and ``tests/router/
test_arch_n3_tier2_classification.py`` pins the exact delta so a future
edit to either has to state its case.

**The public EXACT paths are derived, not declared.** This module has
no literal ``/agent-mcp/api/router/health`` string. Instead
``public_route()`` marks a *handler*, and ``public_paths()`` walks the
already-registered routing table for handlers carrying that mark —
mirroring ``app.py::_add_admin_trailing_slash_aliases``, which derives
its aliases from registered routes rather than a hand-chosen list. Two
consequences that were the point of the exercise:

  * Every mechanically-derived re-registration of the same handler
    (``_add_admin_trailing_slash_aliases``' trailing-slash alias,
    ``_add_root_aliases``' ADR-0020 root mirror) inherits the marking
    for free — nobody has to remember the alias form.
  * The match is EXACT, closing the bug this replaced: the old entry
    was matched with ``path.startswith(p)``, an unbounded prefix, while
    its own comment claimed it was "exact-prefixed". A future
    ``/api/router/health-details`` route would have silently bypassed
    the session gate — the R5-F6 unbounded-prefix-fallthrough class,
    fixed in the ROUTING table but never in this AUTH-BYPASS allowlist.
"""

from __future__ import annotations

import re

from aiohttp import web

from . import mount


# ── Question 1: is this path public? ────────────────────────────────


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
#   * ``/agent-mcp/sso/``: Phase 3 Wave 3 (prancy-napping-pie) — the
#     SSO handshake MUST be reachable without a session cookie, or an
#     operator mid-flow can't complete the redirect dance with the IdP.
#
# The public service descriptor (ADR 0014, ``GET /api/router/health``)
# is deliberately NOT here: it is a single registered ROUTE, not a
# subtree, so it is derived from the routing table by ``public_paths``
# below and matched exactly.
UNAUTH_PREFIXES = (
    "/agent-mcp/login",
    "/agent-mcp/logout",
    "/agent-mcp/setup",
    "/agent-mcp/assets/",
    "/agent-mcp/mcp/",
    "/agent-mcp/sso/",
)


# Exact paths that bypass auth — the bare ``/agent-mcp`` landing
# redirect. We don't list ``/agent-mcp/`` itself because we DO want the
# redirect handler to fire (which 303s to /login or the dashboard
# depending on auth state). The HEAD/GET ``/agent-mcp`` (no trailing
# slash) is the 301 to ``/agent-mcp/``; let it through so we don't
# double-301.
UNAUTH_EXACT = frozenset({"/agent-mcp"})


# Paths that must remain reachable while the users table is empty.
# ``/setup`` is obvious; ``/assets/`` is exempt so the wizard's
# CSS/fonts (none today, but a future PR may add them) load. The
# ``/api/`` and ``/mcp/`` surfaces are exempt because they are
# machine-to-machine (REST API, MCP transport); redirecting them to an
# HTML wizard would break the agent-side bearer flow and every
# pre-Phase-1 dashboard/CI integration that hits the JSON API directly.
# The wizard is HTML-targeted; only HTML-rendering paths need the
# bounce. ADR 0014 retired the ``/__*`` namespace; the admin surface now
# lives under ``/api/router/...`` (covered by the ``/api/`` prefix).
#
# The SSO handshake is exempt for the same reason it is in
# ``UNAUTH_PREFIXES``: a fresh install provisioning its first operator
# via SSO would otherwise be bounced to /setup before the callback ever
# runs (a fail-closed lockout, fixed as one of the plan's Step 0 bugs).
REDIRECT_EXEMPT_PREFIXES = (
    "/agent-mcp/setup",
    "/agent-mcp/assets/",
    "/agent-mcp/api/",
    "/agent-mcp/mcp/",
    "/agent-mcp/sso/",
)


# Attribute stamped on a route handler by ``public_route``. An
# attribute (rather than a registry keyed by path) is what makes the
# marking survive every mechanical re-registration of the same handler
# object.
_PUBLIC_MARKER = "__agent_mcp_public_route__"

# aiohttp app-dict key holding the frozen derived set. Plain string to
# match this package's existing convention (``app.PROXY_TASKS_KEY``).
PUBLIC_PATHS_KEY = "_agent_mcp_public_paths"


def public_route(handler):
    """Mark ``handler`` as reachable WITHOUT an operator session.

    Applied at the registration site, so "this route is public" is a
    fact about the route rather than a string in a parallel allowlist.
    Returns the handler so it can wrap a registration inline::

        app.router.add_get(path, public_route(gated(health_handler)))
    """
    setattr(handler, _PUBLIC_MARKER, True)
    return handler


def _canonicalise(path: str) -> str:
    """Normalise a registered route path into the internal
    ``/agent-mcp`` namespace, the way ``mount.canonical_path`` does for
    a live request — so an ADR-0020 root-mounted alias of a public route
    lands on the same entry as its tailnet twin instead of adding a
    second, root-shaped bypass entry."""
    if path == mount.INTERNAL_MOUNT or path.startswith(
        mount.INTERNAL_MOUNT + "/"
    ):
        return path
    return mount.INTERNAL_MOUNT + path


def derive_public_paths(app: web.Application) -> frozenset[str]:
    """Walk ``app``'s registered routes and return the canonical paths
    whose handler carries the ``public_route`` mark.

    Same idiom as ``app.py::_add_admin_trailing_slash_aliases``: read
    the routing table that already exists rather than maintaining a
    parallel list of path strings by hand.
    """
    paths: set[str] = set()
    for route in app.router.routes():
        resource = route.resource
        if resource is None:
            continue
        if not getattr(route.handler, _PUBLIC_MARKER, False):
            continue
        paths.add(_canonicalise(resource.canonical))
    return frozenset(paths)


def freeze_public_paths(app: web.Application) -> frozenset[str]:
    """Compute the derived public-path set once and stash it on ``app``.

    Called at the END of ``make_app`` — after every alias pass — so the
    per-request gate does an O(1) set lookup instead of re-walking the
    routing table.
    """
    derived = derive_public_paths(app)
    app[PUBLIC_PATHS_KEY] = derived
    return derived


def public_paths(app: web.Application | None) -> frozenset[str]:
    """The derived public-path set for ``app``.

    Falls back to deriving on the fly when ``freeze_public_paths``
    hasn't run (an Application built directly by a test rather than
    through ``make_app``). The fallback re-derives from the SAME routing
    table — there is no hand-maintained default to drift.
    """
    if app is None:
        return frozenset()
    frozen = app.get(PUBLIC_PATHS_KEY)
    if frozen is not None:
        return frozen
    return derive_public_paths(app)


def matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    """Shared prefix matcher for the two policy tuples above."""
    return any(path.startswith(p) for p in prefixes)


def is_unauth_path(path: str, app: web.Application | None = None) -> bool:
    """Return True iff ``path`` (canonical form) skips the
    operator-session gate."""
    if path in UNAUTH_EXACT:
        return True
    if path in public_paths(app):
        return True
    return matches_prefix(path, UNAUTH_PREFIXES)


def is_redirect_exempt(path: str) -> bool:
    """Return True iff ``path`` (canonical form) is exempt from the
    fresh-install bounce to the setup wizard."""
    return matches_prefix(path, REDIRECT_EXEMPT_PREFIXES)


# ── Question 4 (shared by 3): which project is this? ────────────────


# Project-scoped prefixes that REQUIRE project_membership for the
# resolved operator. Captured as regexes so the project slug can be
# extracted in one O(1) match.
PROJECT_SCOPED_PATTERNS = (
    re.compile(r"^/agent-mcp/api/(?P<project>[^/]+)(?:/|$)"),
    re.compile(r"^/agent-mcp/app/(?P<project>[^/]+)(?:/|$)"),
)


# Values of the first ``/api/`` segment that are NOT projects (they're
# router-level admin endpoints). ADR 0014: the single ``router`` segment
# replaces the prior per-route ``projects`` entry — every admin endpoint
# lives at ``/api/router/...`` so this is the only top-level segment we
# have to exempt.
NON_PROJECT_API_SEGMENTS = frozenset({"router"})


def project_segment_from_path(path: str) -> str | None:
    """Return the raw project URL segment if ``path`` is project-scoped,
    else None. Reserved non-project segments (``router``) yield None.

    This is the SYNTACTIC half of "which project is this?" — turning it
    into a real project name (ADR-0010 alias resolution) is the
    registry's job, done by ``auth_middleware._resolved_project_from_path``
    through the same ``app._resolve_project_or_alias`` the proxy uses.
    """
    for pat in PROJECT_SCOPED_PATTERNS:
        m = pat.match(path)
        if m is None:
            continue
        project = m.group("project")
        if project in NON_PROJECT_API_SEGMENTS:
            return None
        return project
    return None


# ── Question 3: is this a delivery route? ───────────────────────────


# ADR-0021 delivery transport: the per-agent fallback channel. These two
# project-scoped routes are authenticated by the AGENT BEARER at the
# backend (``require_agent_bearer``), exactly like the ``/agent-mcp/mcp/``
# transport — NOT by an operator session. So they must skip the
# operator-session gate (the backend does the real auth) AND the
# Accept-version gate (their contract is versioned by the delivery frame
# shape, not the JSON REST media type), while every OTHER
# ``/api/<project>/...`` route keeps both. The set is deliberately tight
# (only ``stream`` and ``status``) so it can't be used to reach any other
# project route unauthed.
DELIVERY_RESTS = frozenset({"delivery/stream", "delivery/status"})


def _strip_one_trailing_slash(rest: str) -> str:
    return rest[:-1] if rest.endswith("/") else rest


def is_delivery(project_segment: str, rest: str) -> bool:
    """Return True iff ``(<project>, <rest>)`` names a delivery route.

    ``rest`` is the backend-facing tail aiohttp's ``{rest:.*}`` capture
    hands ``backend_api_handler``. One trailing slash is tolerated (the
    path-shaped matcher has always tolerated it; this is the side that
    used to disagree). The reserved ``router`` segment is excluded — it
    is the ADR-0014 admin surface, never a project, so a
    ``/api/router/delivery/stream`` path must not inherit the delivery
    carve-out from either gate.
    """
    if project_segment in NON_PROJECT_API_SEGMENTS:
        return False
    return _strip_one_trailing_slash(rest) in DELIVERY_RESTS


_DELIVERY_PATH_RE = re.compile(
    r"^/agent-mcp/api/(?P<project>[^/]+)/(?P<rest>delivery/[^/]+/?)$"
)


def is_delivery_path(path: str) -> bool:
    """Return True iff the canonical ``path`` is a delivery route.

    Splits the path the same way aiohttp's ``/api/{name}/{rest:.*}``
    resource does and delegates to ``is_delivery`` — so the middleware
    (which only has a path) and ``backend_api_handler`` (which has the
    split match_info) can never disagree.
    """
    m = _DELIVERY_PATH_RE.match(path)
    if m is None:
        return False
    return is_delivery(m.group("project"), m.group("rest"))


__all__ = [
    "DELIVERY_RESTS",
    "NON_PROJECT_API_SEGMENTS",
    "PROJECT_SCOPED_PATTERNS",
    "PUBLIC_PATHS_KEY",
    "REDIRECT_EXEMPT_PREFIXES",
    "UNAUTH_EXACT",
    "UNAUTH_PREFIXES",
    "derive_public_paths",
    "freeze_public_paths",
    "is_delivery",
    "is_delivery_path",
    "is_redirect_exempt",
    "is_unauth_path",
    "matches_prefix",
    "project_segment_from_path",
    "public_paths",
    "public_route",
]
