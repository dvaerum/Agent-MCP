"""External mount / URL-prefix derivation (ADR-0020).

The router's routes live under an INTERNAL namespace (``/agent-mcp``),
but the EXTERNAL mount prefix is owned by the reverse proxy and varies
per front door: the tailnet serves the router under ``/agent-mcp/``,
while a Traefik proxy may serve it at the host **root**. The same
process handles both concurrently, so the external prefix + origin must
be derived PER REQUEST — a static config can encode only one mount.

Three surfaces:

- :func:`canonical_path` — the request path in the internal
  ``/agent-mcp`` form, for routing-logic + auth checks. A root-aliased
  request is normalised back to its ``/agent-mcp`` twin so it is gated
  IDENTICALLY. **SECURITY**: the operator-session gate MUST key off this,
  never ``request.path`` — otherwise a root-aliased route skips the
  ``startswith("/agent-mcp")`` gate and serves unauthenticated.
- :func:`external_prefix` — the path prefix the client's browser uses
  (``""`` at root, ``/agent-mcp`` on the tailnet, or whatever a trusted
  proxy declares via ``X-Forwarded-Prefix``), for redirects, the
  dashboard asset prefix, and ``.mcp.json`` snippet URLs.
- :func:`external_origin` — ``scheme://host`` the browser sees.

Trust: ``X-Forwarded-*`` are client-settable, so ``X-Forwarded-Prefix``
/ ``-Host`` / ``-Proto`` are honoured ONLY from a trusted proxy
(``rate_limit.request_from_trusted_proxy`` — the same boundary that
gates ``X-Forwarded-For``). From an untrusted peer we fall back to
transport values + path inference an attacker can't forge (OBS7 class).
"""

from __future__ import annotations

from aiohttp import web

#: The app's internal route namespace. Every route + path-check is
#: expressed relative to this; it is decoupled from the external mount.
INTERNAL_MOUNT = "/agent-mcp"


def _trusted(request: web.Request) -> bool:
    from . import rate_limit

    return rate_limit.request_from_trusted_proxy(request)


def _arrived_under_mount(path: str) -> bool:
    return path == INTERNAL_MOUNT or path.startswith(INTERNAL_MOUNT + "/")


def _norm_prefix(raw: str) -> str:
    """Normalise a prefix to ``""`` or ``/seg[/seg...]`` (no trailing /)."""
    stripped = raw.strip().strip("/")
    return "/" + stripped if stripped else ""


def canonical_path(request: web.Request) -> str:
    """Request path in the internal ``/agent-mcp`` namespace.

    Tailnet requests already arrive under ``/agent-mcp`` (unchanged).
    A root request (proxy stripped the prefix / Traefik mounted at root)
    is normalised to its ``/agent-mcp`` form so every existing path check
    + the auth gate treat it identically to the tailnet twin.
    """
    p = request.path
    if _arrived_under_mount(p):
        return p
    if p == "/":
        return INTERNAL_MOUNT + "/"
    return INTERNAL_MOUNT + p


def external_prefix(request: web.Request) -> str:
    """The URL prefix the client's browser sees (``""`` at root)."""
    if _trusted(request):
        xfp = request.headers.get("X-Forwarded-Prefix")
        if xfp is not None:
            return _norm_prefix(xfp)
    # No trusted declaration: infer from how the request arrived.
    return INTERNAL_MOUNT if _arrived_under_mount(request.path) else ""


def external_origin(request: web.Request) -> str:
    """``scheme://host`` the browser sees.

    Honours ``X-Forwarded-Proto`` / ``-Host`` only from a trusted proxy;
    otherwise the untrusted transport values (which an attacker can't
    forge past the real proxy) — matching ``login._external_origin``.
    """
    proto = request.url.scheme
    host = request.host
    if _trusted(request):
        proto = request.headers.get("X-Forwarded-Proto") or proto
        host = request.headers.get("X-Forwarded-Host") or host
    return f"{proto}://{host}"


def external_path(request: web.Request, internal_suffix: str) -> str:
    """Client-facing path from an internal suffix (the part AFTER the
    mount). e.g. ``/app/foo/`` → ``/agent-mcp/app/foo/`` on the tailnet,
    ``/app/foo/`` at root."""
    suffix = internal_suffix if internal_suffix.startswith("/") \
        else "/" + internal_suffix
    return external_prefix(request) + suffix


def external_url(request: web.Request, internal_suffix: str) -> str:
    """Absolute client-facing URL: origin + external prefix + suffix."""
    return external_origin(request) + external_path(request, internal_suffix)
