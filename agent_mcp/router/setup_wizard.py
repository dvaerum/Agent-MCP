"""First-boot setup wizard + empty-users redirect middleware (PR C).

Companion module to ``login.py``. Owns:

  * ``GET  /agent-mcp/setup``  — render the form (only while the
    users table is empty; otherwise 303 to /login).
  * ``POST /agent-mcp/setup``  — validate, create the first user
    via ``identity.create_user`` (which retroactively grants
    membership in every existing project per PR B's contract),
    create a session, set the cookie, redirect to ``/agent-mcp/``.
  * ``empty_users_redirect_middleware`` — aiohttp middleware that,
    when the users table is empty, redirects ANY ``/agent-mcp/...``
    request EXCEPT ``/agent-mcp/setup`` and ``/agent-mcp/assets/*``
    to ``/agent-mcp/setup``. Without this the operator has no way
    to reach the wizard; they'd see whatever the dashboard route
    serves (likely a 401 from a future PR D dependency, or the
    React SPA's "no project" view today).

The empty-users check hits SQLite on every request. At one
``SELECT 1 FROM users LIMIT 1`` per request the cost is
microseconds-level — well below the proxy hop's overhead — so a
cache is not warranted for Phase 1.
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

from aiohttp import web

from . import identity
from .login import (
    _form_str,
    _render,
    _set_session_cookie,
    enforce_same_origin,
)


logger = logging.getLogger(__name__)


# ── Empty-users check ──────────────────────────────────────────────


def users_table_is_empty() -> bool:
    """Return True iff the ``users`` table has zero rows.

    Returns True (treat as empty) if the table is missing — that's
    the pre-migration state, which presents the same operator-facing
    "you need to set up" UX as a freshly-migrated empty table.

    Delegates to ``store.users_table_is_empty`` — the single empty-table
    probe (arch-deepening R2 #1c). Kept as a module function so the
    middleware, setup handlers, and tests that import it by name keep
    working.
    """
    from .router_store import store

    return store.users_table_is_empty()


# ── Middleware ─────────────────────────────────────────────────────


# Paths that must remain reachable while the users table is empty.
# /setup is obvious; /assets is exempt so the wizard's CSS/fonts (none
# today, but a future PR may add them) load. The /api/ and /mcp/
# surfaces are exempt because they are machine-to-machine (REST API,
# MCP transport); redirecting them to an HTML wizard would break the
# agent-side bearer flow and every pre-Phase-1 dashboard/CI
# integration that hits the JSON API directly. The wizard is
# HTML-targeted; only HTML-rendering paths need the bounce.
#
# ADR 0014 retired the ``/__*`` namespace; the admin surface now lives
# under ``/api/router/...`` (covered by the ``/api/`` prefix).
_REDIRECT_EXEMPT_PREFIXES = (
    "/agent-mcp/setup",
    "/agent-mcp/assets/",
    "/agent-mcp/api/",
    "/agent-mcp/mcp/",
    # The SSO handshake itself must be reachable without a session,
    # same as auth_middleware._UNAUTH_PREFIXES -- else a fresh install
    # (empty users table) provisioning its first operator via SSO gets
    # bounced to /setup before the callback ever runs.
    "/agent-mcp/sso/",
)


@web.middleware
async def empty_users_redirect_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Bounce any /agent-mcp/* HTML request to /setup when no users exist."""
    # ADR-0020: canonicalise so a root-aliased request (Traefik at host
    # root) hits the same exempt-prefix allow-list as its tailnet twin,
    # and redirect to the SETUP page at the client's actual mount prefix.
    from . import mount
    path = mount.canonical_path(request)
    if not path.startswith("/agent-mcp"):
        return await handler(request)
    if any(path.startswith(p) for p in _REDIRECT_EXEMPT_PREFIXES):
        return await handler(request)
    if users_table_is_empty():
        raise web.HTTPSeeOther(location=mount.external_path(request, "/setup"))
    return await handler(request)


# ── Setup handlers ─────────────────────────────────────────────────


def _render_setup_form(
    *,
    error: str | None,
    username: str = "",
    email: str = "",
) -> str:
    return _render(
        "setup.html",
        error=error,
        username=username,
        email=email,
    )


async def setup_get_handler(request: web.Request) -> web.Response:
    """GET /agent-mcp/setup — render the form or bounce to /login."""
    if not users_table_is_empty():
        raise web.HTTPSeeOther(location="/agent-mcp/login")
    html = _render_setup_form(error=None)
    return web.Response(
        text=html, content_type="text/html", charset="utf-8",
    )


async def setup_post_handler(request: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/setup — validate + create the first operator."""
    # Login-CSRF guard (R9-F1): like /login, this POST mints a session
    # cookie (bootstrapping the first operator), so SameSite=Lax does
    # not cover it. The users-empty check below already narrows the
    # window to first-boot, but guarding here keeps this handler in the
    # same cookie-minting class so a future refactor can't reopen it.
    enforce_same_origin(request)
    if not users_table_is_empty():
        # A POST after the wizard's already been completed — most
        # likely a back-button replay. Bounce to /login rather than
        # surfacing a 409, which is the friendlier UX.
        raise web.HTTPSeeOther(location="/agent-mcp/login")

    try:
        form = await request.post()
    except (ValueError, UnicodeDecodeError):
        # A malformed form body (e.g. invalid UTF-8 in a urlencoded
        # payload) makes ``request.post()`` raise ``UnicodeDecodeError``
        # (a ``ValueError`` subclass), which would otherwise propagate to
        # an uncaught 500 in the bootstrap window (PF-R21-1). Fold it into
        # the wizard's existing invalid-input path: the same 400 +
        # re-rendered form the missing-field branches below return.
        return web.Response(
            text=_render_setup_form(
                error="Invalid form submission.",
                username="",
                email="",
            ),
            status=400,
            content_type="text/html",
            charset="utf-8",
        )
    username = _form_str(form, "username").strip()
    password = _form_str(form, "password")
    password_confirm = _form_str(form, "password_confirm")
    email = _form_str(form, "email").strip() or None

    if not username:
        return web.Response(
            text=_render_setup_form(
                error="Username is required.",
                username="",
                email=email or "",
            ),
            status=400,
            content_type="text/html",
            charset="utf-8",
        )

    if not password:
        return web.Response(
            text=_render_setup_form(
                error="Password is required.",
                username=username,
                email=email or "",
            ),
            status=400,
            content_type="text/html",
            charset="utf-8",
        )

    if password != password_confirm:
        return web.Response(
            text=_render_setup_form(
                error="Passwords do not match.",
                username=username,
                email=email or "",
            ),
            status=400,
            content_type="text/html",
            charset="utf-8",
        )

    # Enforce the shared password-strength policy BEFORE create_user, so
    # a self-provisioned first operator can't set a trivially-guessable
    # secret (round-3 finding AC-2). Policy lives in identity so the
    # admin/self-serve paths can share the exact same rule.
    try:
        identity.validate_password_strength(password)
    except identity.WeakPasswordError as exc:
        return web.Response(
            text=_render_setup_form(
                error=str(exc),
                username=username,
                email=email or "",
            ),
            status=400,
            content_type="text/html",
            charset="utf-8",
        )

    try:
        user_id = identity.create_user(
            username=username, password=password, email=email,
        )
    except identity.UsernameAlreadyExistsError:
        # Race: someone else won the wizard between our empty check
        # and the INSERT. Surface as a redirect to /login — the
        # operator can sign in with the credentials they (or their
        # co-operator) just chose.
        raise web.HTTPSeeOther(location="/agent-mcp/login")

    session_id = identity.create_session(user_id)
    identity.touch_last_login(user_id)
    response = web.HTTPSeeOther(location="/agent-mcp/")
    _set_session_cookie(response, request, session_id)
    raise response


# ── Wire-up ────────────────────────────────────────────────────────


def register_setup_routes(app: web.Application) -> None:
    """Register the setup wizard routes.

    The empty-users redirect middleware is wired separately at
    Application construction time (see ``router.app.make_app``);
    aiohttp's middleware chain is frozen once the app starts, so
    mutating ``app.middlewares`` post-hoc would no-op silently.
    """
    app.router.add_get("/agent-mcp/setup", setup_get_handler)
    app.router.add_post("/agent-mcp/setup", setup_post_handler)


# Re-export for callers that want the middleware separately (tests).
__all__ = [
    "empty_users_redirect_middleware",
    "register_setup_routes",
    "setup_get_handler",
    "setup_post_handler",
    "users_table_is_empty",
]
