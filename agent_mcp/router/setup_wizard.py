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
import sqlite3
from typing import Callable, Awaitable

from aiohttp import web

from . import identity
from .login import (
    _render,
    _set_session_cookie,
)


logger = logging.getLogger(__name__)


# ── Empty-users check ──────────────────────────────────────────────


def users_table_is_empty() -> bool:
    """Return True iff the ``users`` table has zero rows.

    Returns True (treat as empty) if the table is missing — that's
    the pre-migration state, which presents the same operator-facing
    "you need to set up" UX as a freshly-migrated empty table.
    """
    try:
        with identity._connect() as conn:
            cur = conn.execute("SELECT 1 FROM users LIMIT 1")
            return cur.fetchone() is None
    except sqlite3.OperationalError:
        # Table missing — schema not yet applied. Same UX path.
        return True


# ── Middleware ─────────────────────────────────────────────────────


# Paths that must remain reachable while the users table is empty.
# /setup is obvious; /assets is exempt so the wizard's CSS/fonts (none
# today, but a future PR may add them) load. The /api/, /mcp/, and
# /__-prefixed surfaces are exempt because they are machine-to-machine
# (REST API, MCP transport, router-internal JSON like __projects /
# __overview); redirecting them to an HTML wizard would break the
# agent-side bearer flow and every pre-Phase-1 dashboard/CI
# integration that hits the JSON API directly. The wizard is
# HTML-targeted; only HTML-rendering paths need the bounce.
_REDIRECT_EXEMPT_PREFIXES = (
    "/agent-mcp/setup",
    "/agent-mcp/assets/",
    "/agent-mcp/api/",
    "/agent-mcp/mcp/",
    "/agent-mcp/__",
)


@web.middleware
async def empty_users_redirect_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Bounce any /agent-mcp/* HTML request to /setup when no users exist."""
    path = request.path
    if not path.startswith("/agent-mcp"):
        return await handler(request)
    if any(path.startswith(p) for p in _REDIRECT_EXEMPT_PREFIXES):
        return await handler(request)
    if users_table_is_empty():
        raise web.HTTPSeeOther(location="/agent-mcp/setup")
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
    if not users_table_is_empty():
        # A POST after the wizard's already been completed — most
        # likely a back-button replay. Bounce to /login rather than
        # surfacing a 409, which is the friendlier UX.
        raise web.HTTPSeeOther(location="/agent-mcp/login")

    form = await request.post()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    password_confirm = form.get("password_confirm") or ""
    email = (form.get("email") or "").strip() or None

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
