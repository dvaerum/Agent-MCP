"""Login + logout views and session-resolver helpers (Phase 1 PR C).

PR B (``agent_mcp.router.identity``) provides the data layer for
router-side operator identity: argon2 hashing, ``users`` /
``sessions`` / ``project_membership`` tables, ``create_user`` /
``create_session`` / ``get_session``. This module wires those
primitives onto the HTTP surface.

Routes exposed (see ``register_login_routes``):

  * ``GET  /agent-mcp/login``  — render the Jinja login form
  * ``POST /agent-mcp/login``  — form-encoded ``username`` /
    ``password``. On success: create a session, set
    ``Set-Cookie: agent_mcp_session=<sid>``, 303-redirect to
    ``/agent-mcp/`` (or to a safely-relative ``?next=`` target).
    On failure: re-render the page with an error and a 401 status.
  * ``POST /agent-mcp/logout`` — drop the session row, clear the
    cookie, 303-redirect to ``/agent-mcp/login``.

Plus two helpers consumed by the empty-users redirect middleware
(in ``setup_wizard.py``) and — eventually — by PR D's
``require_operator_session`` dependency on dashboard routes:

  * ``resolve_current_user(request)`` — returns the user dict for a
    valid session cookie, or None. Does NOT raise; downstream
    handlers decide how to respond to a None.
  * ``touch_session(session_id)`` — slides ``last_used_at`` on the
    session row. PR B's ``get_session`` already does the same thing
    as a side effect of a successful read; this helper exists as a
    named public surface so PR D's auth dependency can ``touch +
    resolve`` without juggling the side-effect contract.

CSRF protection is deliberately deferred to PR D; the
``SameSite=Lax`` cookie attribute provides a meaningful baseline
against cross-origin form posts in the interim.

Cookie ``Secure`` flag: conditional. On for HTTPS, OFF for plain
HTTP. The VM smoke runs over plain HTTP localhost, so the cookie
must still be settable there; behind tailscale TLS the flag is
added. Detection via ``X-Forwarded-Proto`` first, then
``request.url.scheme`` as the fallback.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import identity


logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────


SESSION_COOKIE_NAME = "agent_mcp_session"
COOKIE_PATH = "/agent-mcp/"
# 30 days, mirroring identity.DEFAULT_SESSION_LIFETIME_DAYS so the
# cookie expires no later than the DB row.
COOKIE_MAX_AGE = 60 * 60 * 24 * 30


# ── Jinja environment ──────────────────────────────────────────────


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def _render(template: str, **ctx: Any) -> str:
    """Render a template from ``agent_mcp/router/templates/``."""
    return _jinja_env.get_template(template).render(**ctx)


# ── Cookie helpers ─────────────────────────────────────────────────


def cookie_secure_flag(request: web.Request) -> bool:
    """Return True iff the request arrived over HTTPS.

    Honours ``X-Forwarded-Proto`` first — production deploys terminate
    TLS at nginx / tailscale and forward plain HTTP to the router via
    a Unix socket / loopback. Falls back to ``request.url.scheme`` so
    a direct HTTPS hit (no proxy) also gets the flag.

    Defaults to False so the plain-HTTP VM smoke + local-dev flows
    can still set the cookie; the operator-facing production deploy
    always sets the forwarded-proto header.
    """
    forwarded = request.headers.get("X-Forwarded-Proto", "").lower()
    if forwarded == "https":
        return True
    if forwarded == "http":
        return False
    return request.url.scheme == "https"


def _set_session_cookie(
    response: web.StreamResponse,
    request: web.Request,
    session_id: str,
) -> None:
    """Attach a fresh agent_mcp_session cookie to ``response``."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        path=COOKIE_PATH,
        httponly=True,
        secure=cookie_secure_flag(request),
        samesite="Lax",
        max_age=COOKIE_MAX_AGE,
    )


def _clear_session_cookie(
    response: web.StreamResponse, request: web.Request,
) -> None:
    """Attach a Max-Age=0 clearer for the agent_mcp_session cookie.

    aiohttp's ``del_cookie`` is fine but emits ``Max-Age=0`` plus an
    ``Expires`` in the past; we want a minimal, predictable header
    so the test's Set-Cookie parser stays trivial.
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        "",
        path=COOKIE_PATH,
        httponly=True,
        secure=cookie_secure_flag(request),
        samesite="Lax",
        max_age=0,
    )


# ── Session resolution ─────────────────────────────────────────────


def touch_session(session_id: str) -> None:
    """Slide ``last_used_at`` to now on ``session_id``.

    Idempotent + no-op for missing rows. PR B's ``get_session``
    already slides on read; this helper exists so PR D's auth
    dependency has a named "I confirmed activity" call site
    independent of whether the session row is fetched separately.
    """
    now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with identity._connect() as conn:
        try:
            conn.execute(
                "UPDATE sessions SET last_used_at = ? WHERE session_id = ?",
                (now_iso, session_id),
            )
        except sqlite3.OperationalError:
            # Missing table — router.db migrations haven't run yet, in
            # which case there are no sessions to slide either. Quiet
            # no-op rather than 500ing the request.
            logger.warning(
                "touch_session: sessions table missing; "
                "is router.db initialised?"
            )


def resolve_current_user(request: web.Request) -> dict[str, Any] | None:
    """Return the user dict for ``request``'s session cookie.

    Returns None when:

      * no session cookie is present,
      * the cookie value is empty (post-logout state),
      * the session row is missing or expired,
      * the user row is missing (stale FK — should not happen in
        practice, but handled defensively).

    Side effect: PR B's ``get_session`` slides ``last_used_at`` on a
    successful fetch, so calling this function on a valid session
    keeps it alive.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not session_id:
        return None
    try:
        session = identity.get_session(session_id)
    except sqlite3.OperationalError:
        # Router DB not yet migrated — treat as "no session".
        return None
    if session is None:
        return None
    user = identity.get_user_by_id(session["user_id"])
    return user


# ── next= validation ───────────────────────────────────────────────


def _safe_next(raw: str | None) -> str:
    """Return ``raw`` if safely path-relative under /agent-mcp/, else /.

    Rejects:
      * empty / missing values (fall through to default)
      * protocol-relative URLs (``//host/...``)
      * absolute URLs (anything with ``://``)
      * paths outside ``/agent-mcp/`` — keeps the redirect inside the
        router's URL surface so a compromised query string can't bounce
        the operator to an unrelated mount.
    """
    if not raw:
        return "/agent-mcp/"
    if raw.startswith("//"):
        return "/agent-mcp/"
    if "://" in raw:
        return "/agent-mcp/"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "/agent-mcp/"
    if parsed.scheme or parsed.netloc:
        return "/agent-mcp/"
    if not raw.startswith("/agent-mcp/"):
        return "/agent-mcp/"
    return raw


# ── Handlers ───────────────────────────────────────────────────────


async def login_get_handler(request: web.Request) -> web.Response:
    """GET /agent-mcp/login — render the form.

    If the operator is already authenticated, bounce to ``next=`` (or
    to /agent-mcp/) so a back-button hit doesn't show a useless form.
    """
    if resolve_current_user(request) is not None:
        target = _safe_next(request.rel_url.query.get("next"))
        raise web.HTTPSeeOther(location=target)
    next_url = request.rel_url.query.get("next", "")
    html = _render("login.html", error=None, username="", next=next_url)
    return web.Response(
        text=html, content_type="text/html", charset="utf-8",
    )


async def login_post_handler(request: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/login — validate, set cookie, redirect."""
    form = await request.post()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = request.rel_url.query.get("next", "")

    error_html = _render(
        "login.html",
        error="Invalid username or password.",
        username=username,
        next=next_url,
    )

    if not username or not password:
        return web.Response(
            text=error_html, status=401,
            content_type="text/html", charset="utf-8",
        )

    user = identity.get_user_by_username(username)
    if user is None:
        # Constant-ish behaviour: same status + same copy as a bad
        # password, so a malicious caller can't tell whether the
        # username exists. (We don't go full constant-time here —
        # argon2's verify cost dominates anyway.)
        return web.Response(
            text=error_html, status=401,
            content_type="text/html", charset="utf-8",
        )

    if not identity.verify_password(user["password_hash"], password):
        return web.Response(
            text=error_html, status=401,
            content_type="text/html", charset="utf-8",
        )

    # Auth ok: create a session, set the cookie, redirect.
    session_id = identity.create_session(user["user_id"])
    identity.touch_last_login(user["user_id"])
    target = _safe_next(next_url)
    response = web.HTTPSeeOther(location=target)
    _set_session_cookie(response, request, session_id)
    raise response


async def logout_handler(request: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/logout — drop session, clear cookie, redirect."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
    if session_id:
        try:
            identity.delete_session(session_id)
        except sqlite3.OperationalError:
            logger.warning("logout: sessions table missing on delete")
    response = web.HTTPSeeOther(location="/agent-mcp/login")
    _clear_session_cookie(response, request)
    raise response


# ── Wire-up ────────────────────────────────────────────────────────


def register_login_routes(app: web.Application) -> None:
    """Register login + logout routes on ``app``.

    Called from ``router.app.make_app`` so the URL surface is opt-in
    at app construction time. Idempotent (re-adding the routes raises
    in aiohttp, so don't).
    """
    app.router.add_get("/agent-mcp/login", login_get_handler)
    app.router.add_post("/agent-mcp/login", login_post_handler)
    app.router.add_post("/agent-mcp/logout", logout_handler)
