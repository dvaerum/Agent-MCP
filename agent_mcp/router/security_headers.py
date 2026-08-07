"""Security response headers for every router response.

Threat model: open-internet exposure. The router served no security
response headers at all, so a public deploy inherited none of the
browser-side defences (clickjacking, MIME sniffing, referrer leak,
missing HSTS). This middleware adds them uniformly — including on
error responses and redirects — by wrapping the whole handler chain.

CSP notes (the one header that can break the app):

  The dashboard is a static Next.js export. Its HTML carries inline
  ``<script>`` blocks (Next's hydration runtime, ``self.__next_f``
  pushes) and the Jinja login/setup templates carry an inline
  ``<style>`` block, so a strict ``script-src 'self'`` / ``style-src
  'self'`` would break both surfaces. Static export can't use per-
  response nonces (the files are served verbatim), so we allow
  ``'unsafe-inline'`` for script + style. We deliberately do NOT add
  ``'unsafe-eval'`` — production Next.js export doesn't need it, and
  it's the more dangerous of the two.

  The parts that add real value regardless — ``frame-ancestors
  'none'`` (clickjacking), ``object-src 'none'``, ``base-uri 'self'``,
  ``default-src 'self'`` (no off-origin loads) — are all kept strict.

The whole CSP is overridable via ``AGENT_MCP_CSP`` for operators who
front the router differently or want to tighten it further.

HSTS is emitted ONLY on HTTPS requests (same ``X-Forwarded-Proto`` /
scheme heuristic as ``login.cookie_secure_flag``) so the plain-HTTP
dev / VM smoke doesn't get pinned to HTTPS.

Cache-Control (SC-1): sensitive router surfaces — the login page, authed
API JSON, and 401/error bodies — carried no cache directive, so they
could land in bfcache or a shared cache. We stamp ``no-store`` via
``setdefault`` so those responses opt out of caching, while the static
dashboard handlers (which set their OWN ``Cache-Control`` BEFORE this
middleware runs — ``no-store`` for HTML, ``immutable`` for hash-named
assets) keep their explicit value untouched.

Server banner (SC-2 / SD-3): aiohttp fills a ``Server:
Python/… aiohttp/…`` header at prepare time (``setdefault`` on
``SERVER_SOFTWARE``). That discloses exact framework versions for
CVE-matching on a direct bind (nginx masks it in prod, but this is the
defence-in-depth gap). We assign a neutral, version-free banner
UNCONDITIONALLY (not ``setdefault``) so it's present before aiohttp's
own default fires and wins over it.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from aiohttp import web

from .client_disconnect import client_gone_response, client_is_gone


logger = logging.getLogger(__name__)


# Pragmatic-but-protective default. See module docstring for why
# script/style get ``'unsafe-inline'`` (static Next.js export) while
# everything else stays strict.
_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# Minimal Permissions-Policy: the dashboard needs none of these
# powerful features, so deny them outright.
_DEFAULT_PERMISSIONS_POLICY = (
    "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
)

_HSTS_VALUE = "max-age=63072000; includeSubDomains"

# Neutral, version-free replacement for aiohttp's ``Server:
# Python/… aiohttp/…`` banner (SC-2 / SD-3). Names the product but
# discloses nothing a CVE scanner can pivot on.
_SERVER_BANNER = "agent-mcp"


def _request_is_https(request: web.Request) -> bool:
    """Same heuristic as ``login.cookie_secure_flag``.

    Honours ``X-Forwarded-Proto`` first (TLS terminates upstream and
    forwards plain HTTP), then the direct request scheme. Kept inline
    rather than importing ``login`` so this leaf middleware carries no
    dependency on the login view module.
    """
    forwarded = request.headers.get("X-Forwarded-Proto", "").lower()
    if forwarded == "https":
        return True
    if forwarded == "http":
        return False
    return request.url.scheme == "https"


def _csp() -> str:
    override = os.environ.get("AGENT_MCP_CSP")
    return override if override else _DEFAULT_CSP


def _apply_headers(response: web.StreamResponse, request: web.Request) -> None:
    """Set the security headers on ``response`` (idempotent per response).

    Uses ``setdefault`` so a handler that intentionally set its own
    value (rare) isn't clobbered.
    """
    hdrs = response.headers
    hdrs.setdefault("X-Content-Type-Options", "nosniff")
    hdrs.setdefault("X-Frame-Options", "DENY")
    hdrs.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    hdrs.setdefault("Content-Security-Policy", _csp())
    hdrs.setdefault("Permissions-Policy", _DEFAULT_PERMISSIONS_POLICY)
    # Cross-origin isolation (defense-in-depth alongside frame-ancestors
    # / X-Frame-Options). COOP: same-origin severs cross-origin window
    # handles so a popup/opener can't reach into this context (blocks
    # a class of Spectre-style + tab-nabbing side channels). CORP:
    # same-origin refuses embedding of the router's responses as a
    # cross-origin resource. The dashboard is fully same-origin, so
    # neither breaks any legitimate load.
    hdrs.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    hdrs.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    # HSTS only over HTTPS — never pin a plain-HTTP dev/VM to TLS.
    if _request_is_https(request):
        hdrs.setdefault("Strict-Transport-Security", _HSTS_VALUE)
    # SC-1: opt sensitive surfaces out of caching. ``setdefault`` so the
    # static dashboard handlers keep their own explicit value (they set
    # ``Cache-Control`` on the response BEFORE this middleware runs —
    # ``no-store`` for HTML, ``immutable`` for hash-named assets).
    hdrs.setdefault("Cache-Control", "no-store")
    # SC-2 / SD-3: overwrite aiohttp's version-disclosing ``Server``
    # banner. Direct assignment (NOT ``setdefault``) because aiohttp
    # fills ``SERVER_SOFTWARE`` via its own ``setdefault`` at prepare
    # time — our value must already be present so it wins.
    hdrs["Server"] = _SERVER_BANNER


@web.middleware
async def security_headers_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Attach security headers to every response, including errors.

    aiohttp raises ``HTTPException`` for redirects (login 303s) and
    error responses; those ARE the response object, so we catch, stamp
    the headers, and re-raise to cover the full surface.

    An UNhandled exception (a bare ``ValueError`` from malformed
    multipart, a ``TypeError`` in a handler, …) is NOT an
    ``HTTPException``, so it would otherwise propagate past this
    outermost middleware to aiohttp's core 500 renderer — which never
    runs ``_apply_headers`` (SD-R5-1). That path leaked aiohttp's
    version-disclosing ``Server`` banner and dropped every hardened
    header. We catch it here, log the real exception server-side, and
    return a generic 500 stamped with the full header set — keeping the
    exception detail off the wire.
    """
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _apply_headers(exc, request)
        raise
    except ConnectionError as exc:
        # The client vanished mid-request — an SSE stream whose browser
        # tab closed, an aborted fetch, an abandoned upload. aiohttp
        # signals it as ClientConnectionResetError ("Cannot write to
        # closing transport") on the write side and ConnectionResetError
        # ("Connection lost") on the read side; both subclass
        # ConnectionError.
        #
        # This is the OUTERMOST middleware, so re-raising handed the
        # exception straight to aiohttp's `RequestHandler.handle_error`,
        # which logged a full `aiohttp.server` ERROR traceback for what
        # is a peer leaving — noise indistinguishable from a real
        # failure, and the thing that made every occurrence cost a
        # triage. Absorb it instead and return the client-gone status
        # (nothing reaches a wire; the peer is gone by construction).
        #
        # Gated on the DOWNSTREAM transport actually being gone: a
        # ConnectionError raised while the client is still connected is
        # somebody else's reset (a backend/upstream fault) and keeps its
        # traceback via the generic handler below.
        if client_is_gone(request):
            logger.debug(
                "client disconnected during %s %s: %s",
                request.method, request.rel_url, exc,
            )
            response = client_gone_response()
            _apply_headers(response, request)
            return response
        logger.error(
            "Connection reset during %s %s while the client was still "
            "connected; returning generic 500",
            request.method,
            request.rel_url,
            exc_info=True,
        )
        response = web.Response(status=500, text="Internal Server Error")
        _apply_headers(response, request)
        return response
    except Exception:
        # Log the real cause server-side (with traceback) but NEVER put
        # it in the response body — a generic message keeps internals
        # (and any secret in the exception text) off the wire.
        logger.error(
            "Unhandled exception in %s %s; returning generic 500",
            request.method,
            request.rel_url,
            exc_info=True,
        )
        response = web.Response(status=500, text="Internal Server Error")
        _apply_headers(response, request)
        return response
    _apply_headers(response, request)
    return response


__all__ = ["security_headers_middleware"]
