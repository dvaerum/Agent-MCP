#!/usr/bin/env python3
# ── MOVED-UPSTREAM SOURCE ───────────────────────────────────────────
# Was: nixos-developer-system/users/dennis/agent-mcp/router.py
# Now: agent_mcp/router/app.py — moved upstream in Phase 1a of the
# router-upstream plan (prancy-napping-pie). The deploy repo now
# invokes this via `python -m agent_mcp.cli router …` (the new CLI
# subcommand added in the same PR) instead of carrying its own copy.
#
# Behaviour changes folded into the move:
#   * Removed the stale `<p>SSE URL: <code>{escape(sse)}</code></p>`
#     fragment in `_wiring_help_panel` — it referenced an undefined
#     `sse` variable and raised `NameError` on every render
#     (production 500s visible in journalctl). Streamable HTTP is
#     the only supported transport now, so the line was dead.
#   * `import project_registry` → `from . import project_registry`
#     so the registry resolves as a sibling module inside the
#     `agent_mcp.router` package.
# ────────────────────────────────────────────────────────────────────
"""
agent-mcp-router

Thin always-on HTTP proxy + systemd-lifecycle manager that fronts
per-project agent-mcp backends. Pure passthrough on the MCP +
REST routes: request bodies and response chunks are streamed
unchanged. The router does NOT parse MCP protocol bodies;
tools/list and tools/call are forwarded byte-for-byte.

URL convention: the admin surface lives under
`/agent-mcp/api/router/...` (ADR 0014). Project names occupy every
other segment under `/agent-mcp/`; a single reserved top-level
segment ``router`` (joined by `api`, `app`, `assets`, `mcp` —
defended at validate-name time) carves out the admin namespace so
the two cannot collide.

  GET  /agent-mcp/                            HTML index page / JSON descriptor
  GET  /agent-mcp/api/router/health           public service descriptor
  GET  /agent-mcp/api/router/projects         JSON name list
  POST /agent-mcp/api/router/projects         create project
  PATCH  /agent-mcp/api/router/projects/<n>   rename (body: {name, grace_days?})
  DELETE /agent-mcp/api/router/projects/<n>   unregister
  POST /agent-mcp/api/router/projects/<n>/stop  stop a project (refuses if busy)
  GET  /agent-mcp/api/router/overview         cross-project envelope
  GET  /agent-mcp/api/router/projects/<n>/client-config[?agent=<id>]
  GET  /agent-mcp/api/router/projects/<n>/installer[?agent=<id>]
  GET  /agent-mcp/api/router/projects/<n>/aliases?alias=<a>
  DELETE /agent-mcp/api/router/projects/<n>/aliases/<a>
  POST /agent-mcp/api/router/projects/<n>/agents

  *    /agent-mcp/mcp/<name>                  proxy → backend /mcp
                                              (Streamable HTTP transport,
                                              MCP spec rev 2025-03-26)
  *    /agent-mcp/api/<name>/{rest}           proxy → backend /api/{rest}
  GET  /agent-mcp/app/<name>/{rest}           static Next.js export

dvaerum/Agent-MCP 3.0.0 dropped the SSE+messages pair in favour of
a single stateless Streamable HTTP /mcp endpoint. Sessions are no
longer issued or required; backend restarts are invisible to
clients beyond the in-flight request itself. Phase 6 (router-
upstream plan) removed the transitional 410-Gone handlers for the
old `/agent-mcp/__sse/<name>` and `/agent-mcp/__messages/<name>/...`
URLs — those shapes now 404 via aiohttp's default behaviour.

Backend instances are systemd template units (agent-mcp@<name>.service)
listening on Unix domain sockets at
$XDG_RUNTIME_DIR/agent-mcp/<name>/backend.sock. The router calls
`systemctl --user start agent-mcp@<name>` lazily on first request
and `systemctl --user stop ...` from the idle reaper after
AGENT_MCP_IDLE_SEC of no traffic. The router never spawns
processes itself; systemd owns lifecycle.

Configuration (all via environment variables):

  AGENT_MCP_PROJECTS_FILE        JSON {name: path}. Maintained by
                                 the router itself via POST /
                                 DELETE on /api/router/projects.
  AGENT_MCP_SOCK_DIR             where backend.sock files live
                                 (typically $XDG_RUNTIME_DIR/agent-mcp).
  AGENT_MCP_DASHBOARD_DIR        static Next.js export root.
  AGENT_MCP_ROUTER_PORT          TCP port (default 1337).
  AGENT_MCP_IDLE_SEC             seconds before reaper SIGTERMs an
                                 idle backend (default 14400 = 4 h).
  AGENT_MCP_EXTERNAL_URL         https://<host>.<tailnet>.ts.net —
                                 used to render copy-pastable
                                 wiring-help URLs.
  AGENT_MCP_DEFAULT_WORKSPACE    parent dir for workspaces when the
                                 user doesn't provide one (typically
                                 ~/.local/share/agent-mcp/projects).
  AGENT_MCP_INSTALLER_TEMPLATE   path to the bash installer template
                                 (./installer.sh.in in the source
                                 tree). `__AGENT_MCP_MCP_URL__`
                                 inside it gets substituted with
                                 the project's /mcp endpoint URL
                                 at request time.
"""

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, UnixConnector, web

from . import project_registry  # sibling module — see ./project_registry.py
from . import asset_prefix as _asset_prefix  # Phase 4: runtime sentinel sub
from . import project_orchestrator as _po  # PR-C: lifecycle state machine
from .single_tenant import bypasses_operator_gate  # arch-r4 #8


log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────

# Honour the AGENT_MCP_PROJECTS_FILE env override at module import.
# project_registry reads its own module-global REGISTRY_PATH; we
# pin it here so a single env var still drives both this script and
# (via direct env-var read) project_registry's default.
PROJECTS_FILE = Path(os.environ["AGENT_MCP_PROJECTS_FILE"])
project_registry.REGISTRY_PATH = PROJECTS_FILE

# Shared instance: cheap to construct (no I/O), but a module-level
# handle keeps the import-time path-override above paired with every
# call site. Tests can monkeypatch project_registry.REGISTRY_PATH —
# this instance picks the new value up via its lazy `.path` property.
_REGISTRY = project_registry.ProjectRegistry()
SOCK_DIR = _po.SOCK_DIR
DASHBOARD_DIR = os.environ["AGENT_MCP_DASHBOARD_DIR"]
ROUTER_PORT = int(os.environ.get("AGENT_MCP_ROUTER_PORT", "1337"))
IDLE_SEC = _po.IDLE_SEC
EXTERNAL_URL = os.environ["AGENT_MCP_EXTERNAL_URL"].rstrip("/")
DEFAULT_WORKSPACE_PARENT = Path(
    os.environ.get(
        "AGENT_MCP_DEFAULT_WORKSPACE",
        str(Path.home() / ".local" / "share" / "agent-mcp" / "projects"),
    )
).expanduser()
# AGENT_MCP_README_HTML used to be read here and embedded in the legacy
# server-rendered HTML index page. The index page was deleted in Phase
# 6 (see ``index_handler`` further down); the env var is still
# accepted by the CLI / module for back-compat but the router no longer
# reads it.

# ── Runtime asset prefix (Phase 4) ──────────────────────────────────
# The dashboard build emits a literal sentinel
# (``__AGENT_MCP_ASSET_PREFIX__``) wherever Next.js would normally bake
# ``assetPrefix`` into HTML / JS / CSS bytes. The router substitutes
# this value in at serve time so one build artifact can be deployed at
# any URL prefix without a rebuild.
#
# Default ``/agent-mcp/__dashboard`` preserves the historic URL shape
# the deploy repo already serves under — operators who don't set the
# env var see zero behavior change. Operators deploying behind a
# reverse proxy mounted at a different path override the env var (or
# pass ``--asset-prefix`` to the CLI) to a matching value; no rebuild.
#
# See ADR-0008 + the Phase 4 entry of the plan
# ``prancy-napping-pie.md`` for the design rationale (single build,
# runtime substitution, sentinel-as-self-documenting marker).
ASSET_PREFIX: str = os.environ.get(
    "AGENT_MCP_ASSET_PREFIX", "/agent-mcp/assets"
)

# ── Single-tenant mode (Phase 3) ────────────────────────────────────
# When set, the router runs in N=1 mode (services.agent-mcp.multiTenant
# = false; decision #1, ADR-0008). The same URL surface is exposed,
# but the write endpoints (__create / __unregister / __rename) return
# 410 with a documented JSON body, and proxy/dashboard URLs naming any
# other project are W1-redirected (decision #9) to the configured
# single-tenant project at the same section path.
#
# These start as None (multi-tenant default) and are populated by
# `make_app(single_tenant_name=…, single_tenant_workspace=…)` when the
# CLI passes the flags through. Module-level so the route handlers
# (registered as bare async functions on the aiohttp app) can read
# them without threading state through every handler signature.
SINGLE_TENANT_NAME: str | None = None
SINGLE_TENANT_WORKSPACE: str | None = None


# ── API versioning (PR-A) ───────────────────────────────────────────
# The REST surface under /agent-mcp/__api/ requires an explicit,
# version-pinned Accept header so callers opt in to a known wire
# contract. A future v2 ships under a new media-type subtype
# (vnd.agent-mcp.v2+json) while v1 callers keep working unchanged.
#
# Design notes (locked in /grill-me, recorded in URL-redesign plan):
#   * Strict match — Accept: application/json, */* etc. are 406. We
#     want explicit consent, not an inferred default, so that adding
#     v2 doesn't silently break "any JSON" clients.
#   * Parameters allowed — Accept: application/vnd.agent-mcp.v1+json;q=0.9
#     is fine. Per RFC 7231 §5.3.2 the q parameter is informational
#     only at the server side.
#   * The MCP transport at /agent-mcp/<name>/mcp is NOT gated by this
#     header — MCP has its own version negotiation in initialize.
#   * Dashboard HTML / asset routes are NOT gated — browser fetches
#     don't send our private media type, and gating them would break
#     the dashboard.
#   * Folds in audit §3.7: the tokens endpoint's bearer check is
#     bypassed when no Authorization header is sent. This Accept gate
#     runs first, so an unversioned + unauthenticated request fails
#     at 406 before reaching the tokens endpoint at all.
API_VERSION_CURRENT = "v1"
API_SUPPORTED_VERSIONS = ("v1",)
API_MEDIA_TYPE = "application/vnd.agent-mcp.v1+json"
_API_DOCS_URL = (
    "https://github.com/dvaerum/Agent-MCP/blob/main/docs/api-versioning.md"
)


def _accept_includes_strict_api_media(accept_header: str) -> bool:
    """Return True iff Accept lists ``application/vnd.agent-mcp.v1+json``.

    Handles three shapes:
      * bare: ``application/vnd.agent-mcp.v1+json``
      * with parameters (RFC 7231 §5.3.2): ``…;q=0.9``
      * inside a comma-separated list: ``text/plain, application/vnd.agent-mcp.v1+json``

    Anything else — including ``application/json``, ``*/*``, missing —
    returns False. We deliberately do not honour wildcards; an explicit
    opt-in is the point of the gate.
    """
    if not accept_header:
        return False
    for raw_part in accept_header.split(","):
        media_type = raw_part.split(";", 1)[0].strip().lower()
        if media_type == API_MEDIA_TYPE:
            return True
    return False


def _api_version_required_response() -> web.Response:
    """406 body when the Accept-header gate refuses a request.

    Body shape is the public API-versioning error contract — clients
    parse ``error == "version_required"`` to know they should retry
    with the supported media type, and ``supported_versions`` /
    ``current_default`` let an upgrade path light up automatically the
    moment v2 ships. ``message`` is the human-readable single-line
    diagnostic; it contains the exact header value the caller should
    add so operators don't have to read docs to fix the request.
    """
    return web.json_response(
        {
            "error": "version_required",
            "message": (
                "agent-mcp REST endpoints require an Accept header "
                "specifying the API version. Resend with: "
                f"Accept: {API_MEDIA_TYPE}"
            ),
            "supported_versions": list(API_SUPPORTED_VERSIONS),
            "current_default": API_VERSION_CURRENT,
            "docs": _API_DOCS_URL,
        },
        status=406,
    )


def unknown_project_response(req: web.Request) -> web.StreamResponse:
    """The response an ``/api/<project>/`` request gets for a project
    the router does not serve.

    Single source of truth for the "unknown project" wire shape so the
    auth middleware can hand back the SAME thing for a project the
    caller has no membership in — closing the cross-tenant project-
    existence oracle (round 3, PF-1). Without it, an existing-but-not-a-
    member project answered a name-reflecting 401 while a nonexistent
    one answered 404 "unknown project"; that differential let any
    authenticated user enumerate other tenants' slugs (the class SEC5
    already closed on ``/mcp``).

    Reproduces ``backend_api_handler``'s own decision ORDER so the two
    cases stay byte-identical: the Accept-version gate fires first
    (returns a 406 ``Response``), then the unknown-project 404 (RAISED
    as ``HTTPNotFound`` exactly the way ``project_orchestrator._ensure``
    does, so it flows through the same middleware chain and renders
    identically). Neither body reflects the project name — the 404
    reason is a fixed phrase.
    """
    if req.method != "OPTIONS" and not _accept_includes_strict_api_media(
        req.headers.get("Accept", "")
    ):
        return _api_version_required_response()
    raise web.HTTPNotFound(reason="unknown project")


def _accept_prefers_html(accept_header: str) -> bool:
    """Return True iff Accept lists text/html (or an */* aliased to it).

    Used by ``index_handler`` to decide between the JSON service
    descriptor and the legacy 302 → /__dashboard/ redirect. The
    browser → HTML side of the split. ``*/*`` does NOT count — a
    generic API client sending ``*/*`` gets JSON.
    """
    if not accept_header:
        return False
    for raw_part in accept_header.split(","):
        media_type = raw_part.split(";", 1)[0].strip().lower()
        if media_type in ("text/html", "application/xhtml+xml"):
            return True
    return False


# ── Service descriptor (PR-A) ───────────────────────────────────────
# ``GET /agent-mcp/`` returns a small JSON document describing the
# public URL surface so a plain-HTTP client can discover the endpoint
# layout without scraping HTML or hard-coding paths. PR-A surfaces the
# CURRENT shape (still under __api / __dashboard); PR-B's rename
# updates the embedded URLs to the new top-level prefixes.
#
# SEC (owner-authorised, defensive): the internal package version is no
# longer read or published anywhere in the router. It was previously
# echoed by ``_service_descriptor`` and the public ``/health`` probe;
# both were pure attacker-useful build fingerprinting that no operator
# surface consumed (the dashboard hard-codes its own product string).
# The ``_read_package_version`` helper was removed with its only two
# callers — reintroduce it (behind a sysadmin gate) if a build-version
# surface is ever genuinely needed.


def _single_tenant_disabled_response() -> web.Response:
    """410 body shared by the three disabled write endpoints.

    Body shape is locked by the dashboard contract (Phase 3.5):
    ``{ error: "endpoint_disabled_in_single_tenant_mode",
        single_tenant_name: "<name>" }``.
    The dashboard surfaces ``single_tenant_name`` next to the error so
    operators see which project is the configured one without having
    to grep the home-manager config.
    """
    body = {
        "error": "endpoint_disabled_in_single_tenant_mode",
        "single_tenant_name": SINGLE_TENANT_NAME,
    }
    return web.Response(
        status=410,
        body=json.dumps(body).encode("utf-8"),
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
# Installer template: env var overrides (deploy repo still sets this
# explicitly for now); the default falls back to the packaged
# installer.sh.in shipped alongside this module. Resolving via
# importlib.resources keeps the package re-locatable (wheel install,
# nix store path, etc.).
_INSTALLER_TEMPLATE_ENV = os.environ.get("AGENT_MCP_INSTALLER_TEMPLATE")
if _INSTALLER_TEMPLATE_ENV:
    INSTALLER_TEMPLATE_PATH = Path(_INSTALLER_TEMPLATE_ENV)
    _INSTALLER_TEMPLATE = INSTALLER_TEMPLATE_PATH.read_text()
else:
    from importlib.resources import files as _pkg_files
    _packaged_installer = _pkg_files("agent_mcp.router").joinpath("installer.sh.in")
    INSTALLER_TEMPLATE_PATH = Path(str(_packaged_installer))
    _INSTALLER_TEMPLATE = _packaged_installer.read_text()

# Slug regex used to validate project names. Single-letter names
# are allowed (`^[a-z]$`); longer names must start with a letter and
# end with an alphanumeric. Hyphens permitted in the middle.
# `_` is NOT in the character class — that's how the __operation
# namespace is structurally protected from project-name collisions.
_SLUG_RE = re.compile(r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$")
_NAME_MAX = 64

# ── Lifecycle state — owned by project_orchestrator (PR-C) ──────────
# These names are re-exported from the orchestrator module so existing
# handlers below (and tests that monkeypatch ``router_module.<name>``)
# keep working without churn. They are bound to the same module-level
# objects in ``project_orchestrator``; mutations from either side are
# visible to the other because the underlying values are mutable
# (dict, defaultdict, dict-of-locks) and we never rebind the names.
last_active = _po.last_active
active_conns = _po.active_conns
ensure_locks = _po.ensure_locks
# P005 cascade-fix re-export (2026-06-19). The orchestrator caches
# recent ``_ensure`` failures here so a queued first-paint fan-out
# doesn't pay N × socket-wait behind a backend that's failing to come
# up. Tests reach for the dict directly via the router-module surface.
ensure_failures = _po.ensure_failures
_clear_ensure_failures = _po._clear_ensure_failures
_ensure_lock = _po._ensure_lock
_track_connection = _po._track_connection


@contextlib.asynccontextmanager
async def _track_proxy_task(app: web.Application):
    """Register the calling task in the app's proxy-task set.

    The `_drain_proxy_tasks` shutdown hook (registered on
    `app.on_shutdown`) cancels every task in this set on SIGTERM —
    that's how an in-flight `_proxy_to_backend` blocked on
    `await up.read()` gets nudged out of its read instead of waiting
    for aiohttp's `shutdown_timeout` (default 60 s, which used to
    overshoot systemd's `TimeoutStopSec` and earn the router a
    SIGKILL on every deploy).

    Self-cleaning on exit so the set stays bounded — completion,
    error, and cancellation all run through the `finally`.
    """
    tasks = _proxy_task_set(app)
    current = asyncio.current_task()
    if current is not None:
        tasks.add(current)
    try:
        yield
    finally:
        if current is not None:
            tasks.discard(current)


# Per-project agent-token cache. Keyed by project name, value is
# (expires_at, {token → agent_id}). Sole remaining consumer is
# ``_resolve_agent_token`` — the operator-facing dashboard wiring
# endpoints (client-config / installer) need to look up "what token
# does agent X have?" so they can bake it into the .mcp.json snippet
# they hand back. A 3-second TTL is plenty for that interactive
# dashboard click path.
#
# The MCP transport's ``backend_mcp_handler`` used to consult this
# cache to pre-validate per-agent bearers; F015 removed that check
# because the backend's ``AuthHeaderMiddleware`` (which gates ``/mcp``
# against ``g.active_agents``) is the authoritative source of "is this
# bearer live?". See ``backend_mcp_handler`` for the full rationale.
_token_cache_ttl_sec = 3.0
_agent_token_cache: dict[str, tuple[float, dict[str, str]]] = {}


async def _agent_token_map(name: str) -> dict[str, str]:
    """{token: agent_id} for project `name`, freshly cached.

    Holds ONLY per-agent worker/manager tokens — the per-principal
    credentials issued via ``POST /api/agents`` and stored in the
    project's ``agents.token`` column. Sourced from the backend's
    ``GET /api/tokens`` over the project UDS.

    retire-system-token Wave 2 (2026-06-23): the legacy ``Admin``
    pseudo-entry that mapped the per-project system token to the
    string ``"Admin"`` is GONE. The cookie-authenticated dashboard
    path no longer needs an admin bearer to forward upstream — it
    signs a per-request ``X-Agent-MCP-Forwarded-Operator`` header
    instead (see ``_forwarding_header_from_cookie``). Wave 3 deleted
    the per-project system-token file the router used to read.

    F015 (this commit): the MCP transport handler no longer consults
    this map — ``g.active_agents`` is the single source of truth for
    bearer validity. Sole remaining caller is ``_resolve_agent_token``
    for the dashboard wiring/installer endpoints.

    Returns {} on backend error rather than raising; callers are
    expected to treat that as "no agent token found" via the
    empty mapping.
    """
    cached = _agent_token_cache.get(name)
    now = time.time()
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        sock = await _ensure(name, "backend")
    except web.HTTPException:
        return {}
    connector = UnixConnector(path=str(sock))
    timeout = ClientTimeout(total=10)
    try:
        async with ClientSession(connector=connector, timeout=timeout) as sess:
            async with sess.get("http://localhost/api/tokens") as r:
                if r.status != 200:
                    return {}
                body = await r.json()
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    for row in body.get("agent_tokens", []) or []:
        tok = row.get("token")
        aid = row.get("agent_id")
        if tok and aid:
            mapping[tok] = aid
    _agent_token_cache[name] = (now + _token_cache_ttl_sec, mapping)
    return mapping


_BEARER_RE = re.compile(r"^\s*Bearer\s+([A-Za-z0-9._\-]+)\s*$")

def _extract_bearer(req: web.Request) -> str | None:
    """Pull the bearer token from a `Authorization: Bearer …` header."""
    raw = req.headers.get("Authorization") or req.headers.get("authorization")
    if not raw:
        return None
    m = _BEARER_RE.match(raw)
    return m.group(1) if m else None


def _unauthorized() -> web.HTTPException:
    """401 with a WWW-Authenticate header so MCP clients see the cause."""
    return web.HTTPUnauthorized(
        reason="invalid or missing agent bearer token",
        headers={"WWW-Authenticate": 'Bearer realm="agent-mcp"'},
    )


# SEC (owner-authorised, defensive) FINDING 1 [MED] — constant-time
# floor for pre-auth 401s on the /mcp transport. The status CODE and
# ENVELOPE are already unified (PR #279 / SEC5), but the LATENCY was
# not: an UNKNOWN project collapses to 401 in-process (~fast) while a
# KNOWN project with an unvalidated bearer incurs a full backend UDS
# round-trip before ITS 401 collapses (~slow). That delta let a
# not-yet-authenticated caller enumerate valid project names by timing.
# Floor every pre-auth 401 to a fixed wall-clock target measured from
# request receipt so known and unknown return at ~the same time.
# Authenticated (2xx / any non-401) responses are NEVER floored. The
# floor is comfortably above the observed backend round-trip (~14 ms)
# so the round-trip is absorbed rather than observable. Module-level so
# tests can monkeypatch it.
_PREAUTH_401_FLOOR_SEC = 0.05

# SEC (owner-authorised, defensive) FINDING 2 [LOW-MED] — explicit
# request-body cap. aiohttp's default is a silent 1 MiB; make it
# explicit so the limit is intentional and so ``backend_mcp_handler``
# can collapse an oversized-body 413 into the uniform pre-auth 401
# (a 413 that fires only for a KNOWN project is itself an existence
# oracle — see the handler).
_MCP_MAX_BODY_BYTES = 1024 * 1024


async def _floored_unauthorized(t0: float) -> web.HTTPException:
    """Return the canonical pre-auth 401, but not before the timing
    floor (measured from ``t0``, a ``time.monotonic()`` reading taken
    at handler entry) has elapsed.

    Closes the SEC5 timing oracle: the known path (which paid for a
    backend round-trip) and the unknown path (in-process) both return
    at ~``_PREAUTH_401_FLOOR_SEC``. See that constant.
    """
    remaining = _PREAUTH_401_FLOOR_SEC - (time.monotonic() - t0)
    if remaining > 0:
        await asyncio.sleep(remaining)
    return _unauthorized()


# RFC 7230 §6.1 hop-by-hop headers — meaningful only for a single
# transport connection and MUST NOT be forwarded by a proxy. We strip
# them from the request we build for the backend. The load-bearing one
# is ``Transfer-Encoding``: aiohttp's client sets Content-Length from
# the materialised ``data=`` body, so a forwarded ``Transfer-Encoding:
# chunked`` (which arrives on a genuinely chunked inbound POST /mcp)
# collides with it and the backend rejects the request with a hard 4xx
# BadHttpMessage instead of serving it. ``proxy-*`` is matched by prefix
# (covers Proxy-Authenticate / Proxy-Authorization and any vendor
# Proxy- header). Names are compared lowercased.
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})


def _is_hop_by_hop_header(name: str) -> bool:
    """True iff ``name`` is an RFC 7230 hop-by-hop header a proxy must
    not forward (the fixed set above, or any ``Proxy-*`` header)."""
    lowered = name.lower()
    return lowered in _HOP_BY_HOP_HEADERS or lowered.startswith("proxy-")


# ── Project file helpers ─────────────────────────────────────────────


def _projects_dict() -> dict[str, str]:
    """Return `{name: workspace}` snapshot under LOCK_SH.

    Thin compatibility shim over `_REGISTRY.list()` so the existing
    call sites (`name in dict`, `dict.keys()`, `dict[name]`,
    `dict.items()`) keep working while we migrate. The race
    Candidate B targets is on the WRITE path (create_handler's
    read→validate→write window without a lock); reads remain the
    same dict-shaped snapshot they always were, just under LOCK_SH
    now so they can never observe a torn JSON document.
    """
    return {row["name"]: row["workspace"] for row in _REGISTRY.list()}


# Reserved project names. After PR-B's URL rename four segments are
# top-level paths on the router (``api``, ``app``, ``assets``,
# ``mcp``); a project literally named after one of them would become
# structurally unreachable behind the more-specific route registration
# (audit §2.6). ADR 0014 adds ``router`` to the set — it's the single
# admin-namespace segment under ``/api/router/...`` and a project of
# that name would shadow ``/api/router/projects/<router>/...``.
# Rejected at create / rename time so the registry never holds a name
# that would collide.
_RESERVED_NAMES = frozenset({"api", "app", "assets", "mcp", "router"})


def _validate_name(name: str, existing: dict[str, str]) -> str | None:
    """Return None if valid, otherwise a human-readable error."""
    if not name:
        return "name is required"
    if len(name) > _NAME_MAX:
        return f"name is longer than {_NAME_MAX} characters"
    if not _SLUG_RE.match(name):
        return (
            f"name must match {_SLUG_RE.pattern} — lowercase letters, "
            "digits, and hyphens only; first char is a letter, no "
            "leading/trailing hyphen, no underscores (single letter ok)"
        )
    if name in _RESERVED_NAMES:
        return (
            f"name {name!r} is reserved — it conflicts with the "
            f"top-level router path /agent-mcp/{name}/. Reserved names: "
            f"{', '.join(sorted(_RESERVED_NAMES))}."
        )
    if name in existing:
        return f"project {name!r} is already registered"
    return None


# ── Backend lifecycle helpers ────────────────────────────────────────
# Delegated to ``project_orchestrator`` (PR-C). ``_sock_path`` /
# ``_unit_name`` are pure functions aliased for local call sites.
#
# ``_systemctl`` / ``_is_active`` are NOT re-bound here: the seam lives
# in ONE module (``project_orchestrator``) so a test patches a single
# ``monkeypatch.setattr(project_orchestrator, "_systemctl", stub)``.
# This module's call sites reach it as ``_po._systemctl`` /
# ``_po._is_active`` so the patch takes effect without a second binding.
_sock_path = _po._sock_path
_unit_name = _po._unit_name


async def _ensure(name: str, role: str) -> Path:
    """Make sure the backend for (name, role) is running, return its sock.

    Thin re-export of ``project_orchestrator._ensure`` — see that
    function's docstring for the lock / retry / last_active write
    semantics.
    """
    return await _po._ensure(name, role)


# ── Backend proxy ────────────────────────────────────────────────────


def _resolve_project_or_alias(name: str) -> tuple[str, dict | None]:
    """Return (real_name, alias_entry) for the URL segment `name`.

    PR-C: thin delegation to ``ProjectOrchestrator.resolve``. The
    state machine lives in ``project_orchestrator``; this module-level
    wrapper survives so the proxy handlers below (which read the
    module-level ``_REGISTRY``) don't need to know about the
    orchestrator instance.
    """
    return _po.ProjectOrchestrator(_REGISTRY).resolve(name)


async def _proxy_to_backend(
    req: web.Request, name: str, backend_path: str,
    *, alias_info: tuple[str, str] | None = None,
    inject_bearer: str | None = None,
    inject_header: tuple[str, str] | None = None,
) -> web.StreamResponse:
    """Proxy `req` to the backend for `name`, asking it for `backend_path`.

    `backend_path` is the path the agent-mcp backend itself expects
    (e.g. `/mcp`, `/api/agents`). The caller is responsible for
    translating the *router's* URL shape into the *backend's* URL
    shape.

    Pure passthrough: bodies and response chunks are streamed
    unchanged. dvaerum/Agent-MCP 3.0.0 (Streamable HTTP transport)
    no longer requires the SSE-handshake `data: /messages/` byte
    rewrite that this function used to do — the /mcp endpoint is
    URL-stable, so request and response are forwarded verbatim.

    ``inject_header`` is the retire-system-token Wave 2 hook for the
    dashboard's cookie-authenticated MCP path: the cookie path in
    ``backend_mcp_handler`` signs a per-request
    ``X-Agent-MCP-Forwarded-Operator`` header here and we attach it to
    the upstream request so the backend's ``AuthHeaderMiddleware``
    (Wave 1) verifies the HMAC against the per-project key. A
    caller-supplied header of the same name is replaced — the router
    is the only authoritative signer.

    ``inject_bearer`` predates Wave 2 (PR #207 cookie→bearer path).
    Wave 2 stopped using it; the parameter survives so Wave 3 can
    delete it in one mechanical pass without churning this signature
    across the test suite first. No production caller passes it.
    """
    sock = await _ensure(name, "backend")
    url = f"http://localhost{backend_path}"
    # Forward Authorization upstream — the fork's AuthHeaderMiddleware
    # (dvaerum/Agent-MCP#19) reads `Authorization: Bearer <token>` into
    # a ContextVar and injects into arguments.token when the JSON-RPC
    # body doesn't include one. Without forwarding the header, the
    # upstream fallback never triggers.
    #
    # Strip the forwarding-header name unconditionally: the router is
    # the ONLY authoritative source of this header's value, and the
    # backend's middleware treats a present-but-invalid header as
    # 401-worthy. A client-attached value that survives proxying would
    # both (a) DoS legitimate bearer-only requests (the backend 401s
    # the bad HMAC before even reading Authorization) and (b) — in a
    # key-compromise scenario — let an attacker re-attribute requests
    # through the bearer path. Defense-in-depth: strip first, optionally
    # re-inject below when ``inject_header`` is set.
    from ..app import forwarding_header as _fh
    _forwarding_header_lower = _fh.HEADER_NAME.lower()
    headers = {
        k: v for k, v in req.headers.items()
        if k.lower() not in ("host", "content-length", _forwarding_header_lower)
        and not _is_hop_by_hop_header(k)
    }
    if inject_bearer is not None:
        # Strip any caller-supplied Authorization (case-insensitive)
        # before injecting — the cookie path explicitly opts into
        # the admin bearer; a stray lowercase ``authorization`` header
        # must not shadow it.
        for k in list(headers.keys()):
            if k.lower() == "authorization":
                headers.pop(k, None)
        headers["Authorization"] = f"Bearer {inject_bearer}"
    if inject_header is not None:
        # The router OWNS the forwarding header. Any client-supplied
        # value was already stripped during the headers-dict
        # construction above; here we just attach the router-signed
        # value. We do NOT re-strip by inject_header[0] because the
        # initial copy already removed every header that could carry
        # the name (case-insensitive), but defensively pop anyway in
        # case a future caller passes a different inject_header name.
        h_name, h_value = inject_header
        wanted_lower = h_name.lower()
        for k in list(headers.keys()):
            if k.lower() == wanted_lower:
                headers.pop(k, None)
        headers[h_name] = h_value
    # Phase 1b: if this request arrived on an alias URL, tell the
    # backend so it can later (Phase 1c) inject a deprecation warning
    # into the MCP `serverInfo.instructions` field. The header shape
    # is "<alias_name>,<expires_at>" so the backend gets both halves
    # in a single header read.
    if alias_info is not None:
        headers["X-Agent-MCP-Alias"] = f"{alias_info[0]},{alias_info[1]}"
    timeout = ClientTimeout(total=None, sock_read=None)
    connector = UnixConnector(path=str(sock))

    # No byte rewrite needed under Streamable HTTP — /mcp is a
    # single stable URL, so requests and responses (including the
    # POST SSE-body shape for tools that stream progress) pass
    # through verbatim.

    # Materialise the request body up front. The previous streaming-
    # response code path interleaved request writes with response
    # reads; the pure pass-through `await up.read()` below doesn't, so
    # a streamed `data=req.content` could finish with an empty body
    # upstream. Reading first guarantees the full body reaches the
    # backend before we wait on its response.
    req_body = await req.read()
    # R8-F2: `GET /mcp` is the SSE verb/path — the backend answers it
    # with an INFINITE `text/event-stream` (a `: ping` heartbeat every
    # 15 s, forever). Buffering that with `await up.read()` never
    # returns: the client never sees headers/frames, the router→backend
    # UDS conn stays ESTABLISHED, and `_track_connection` never
    # decrements `active_conns` — so ONE abandoned SSE pins the backend
    # against the idle reaper for the agent bearer's whole lifetime.
    # Detect streaming up front (the SSE verb/path) and cap concurrency
    # BEFORE opening the upstream; the response Content-Type is a
    # belt-and-suspenders second signal below for any streaming POST.
    is_stream_request = req.method == "GET" and backend_path == "/mcp"
    stream_cap = (
        _po._track_streaming_proxy(_sse_agent_key(headers))
        if is_stream_request
        else contextlib.nullcontext(True)
    )
    async with _track_proxy_task(req.app), _track_connection(name):
        async with stream_cap as admitted:
            if not admitted:
                # Over the per-agent / global concurrent-SSE cap. Reject
                # cleanly instead of hanging or leaking another pinned
                # upstream. See `_po._track_streaming_proxy`.
                return _too_many_streams_response()
            async with ClientSession(
                connector=connector, timeout=timeout,
            ) as sess:
                async with sess.request(
                    req.method,
                    url,
                    headers=headers,
                    data=req_body,
                    params=req.rel_url.query,
                ) as up:
                    last_active[(name, "backend")] = time.time()
                    ctype = up.headers.get("Content-Type", "")
                    is_streaming = (
                        is_stream_request
                        or ctype.startswith("text/event-stream")
                    )
                    out_headers = {
                        k: v for k, v in up.headers.items()
                        if k.lower()
                        not in ("transfer-encoding", "content-length")
                    }
                    if is_streaming:
                        # Framing-preserving pump: forward chunks as they
                        # arrive so bytes flow to the client incrementally
                        # instead of buffering an unbounded body. The
                        # upstream's lifetime is tied to the client —
                        # returning from here (client gone, or upstream
                        # ended) exits the `async with sess.request(...)`
                        # scope, closes `up`, and lets `_track_connection`
                        # decrement so the reaper works again.
                        return await _stream_upstream_to_client(
                            req, up, name, out_headers,
                        )
                    # Pure pass-through for terminating responses: read
                    # the full upstream body and hand it back as a
                    # complete `web.Response`. aiohttp then serialises a
                    # correct Content-Length with no chunked-transfer
                    # involvement, so strict HTTP clients (curl, Claude
                    # Code's MCP client) see a cleanly-terminated
                    # transfer. A client that drops mid-`await up.read()`
                    # raises ConnectionResetError out of this scope, which
                    # tears the upstream down via the same context exit.
                    body = await up.read()
                    return web.Response(
                        body=body, status=up.status, headers=out_headers,
                    )


def _sse_agent_key(headers: dict[str, str]) -> str:
    """Per-agent cap key for a streaming proxy: a short hash of the
    forwarded bearer (never the raw token, for log/hygiene safety).

    `GET /mcp` always carries a bearer — the cookie-only GET path is
    405'd upstream in `backend_mcp_handler` — so the ``"anon"`` fallback
    is defensive only.
    """
    auth = headers.get("Authorization", "")
    if not auth:
        return "anon"
    return hashlib.sha256(auth.encode("utf-8", "replace")).hexdigest()[:16]


def _too_many_streams_response() -> web.Response:
    """Clean 429 for a streaming proxy rejected at the concurrency cap.

    Fixed, project-agnostic body/reason — never reflect the caller's
    project name (SEC4 pattern) and give no existence oracle beyond the
    401/404 gates the request already cleared.
    """
    return web.Response(
        status=429,
        headers={"Retry-After": "5"},
        text="Too many concurrent streams; retry shortly.",
    )


# R9-F3: how often the independent disconnect watcher polls the
# downstream transport for a client FIN/RST, decoupled from the
# backend's ~15 s SSE heartbeat. This is the WORST-CASE teardown
# latency after the client leaves — small enough that a reconnecting
# agent's slot frees almost immediately, large enough to be negligible
# overhead per live stream. Overridable for tests / unusual deployments.
_SSE_DISCONNECT_POLL_SEC = float(
    os.environ.get("AGENT_MCP_SSE_DISCONNECT_POLL_SEC", "0.5")
)


async def _stream_upstream_to_client(
    req: web.Request,
    up,
    name: str,
    out_headers: dict[str, str],
) -> web.StreamResponse:
    """Pump an SSE (`text/event-stream`) upstream to the client chunk by
    chunk, tying the upstream's lifetime to the downstream client.

    The backend's `GET /mcp` stream never ends on its own and only
    writes on its ~15 s heartbeat, so a write-failure alone is a
    heartbeat-bound disconnect signal: a client that leaves mid-interval
    would pin its concurrency slot (R8-F2 cap) for up to one heartbeat.
    To free the slot on the actual TCP FIN/RST instead, we race the pump
    against an INDEPENDENT disconnect watcher that polls the downstream
    transport (`request.transport.is_closing()`) on its own short
    interval (`_SSE_DISCONNECT_POLL_SEC`), decoupled from the heartbeat.
    Whichever finishes first cancels the other; returning here exits the
    caller's `async with sess.request(...)` scope, closes `up`, and lets
    `_track_connection` (and the streaming-proxy cap) decrement — within
    ~`_SSE_DISCONNECT_POLL_SEC` of the disconnect, not a heartbeat.

    A write to a departed client still raises `ConnectionResetError`
    (or the pump task is cancelled); that path is preserved as a second,
    write-side disconnect signal alongside the watcher.
    """
    resp = web.StreamResponse(status=up.status, headers=out_headers)
    await resp.prepare(req)

    async def _pump() -> None:
        async for chunk in up.content.iter_any():
            last_active[(name, "backend")] = time.time()
            await resp.write(chunk)
        # Upstream ended naturally (rare for /mcp). Flush a clean EOF;
        # the client may already be gone, so a reset here is non-fatal.
        with contextlib.suppress(ConnectionResetError):
            await resp.write_eof()

    async def _watch_disconnect() -> None:
        # Independent of the upstream's write cadence: a FIN/RST flips
        # `transport.is_closing()` on the read side, which this loop sees
        # within one poll interval even while the backend is silent.
        while True:
            transport = req.transport
            if transport is None or transport.is_closing():
                return
            await asyncio.sleep(_SSE_DISCONNECT_POLL_SEC)

    pump = asyncio.ensure_future(_pump())
    watcher = asyncio.ensure_future(_watch_disconnect())
    try:
        await asyncio.wait(
            {pump, watcher}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Cancel the loser (and, on the caller unwinding, both). Then
        # re-await the pump so a benign client-gone error is swallowed
        # here while any genuine pump failure still propagates.
        watcher.cancel()
        if not pump.done():
            pump.cancel()
        with contextlib.suppress(
            asyncio.CancelledError, ConnectionResetError,
        ):
            await pump
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    return resp


# ── Streamable HTTP transport ────────────────────────────────────
# dvaerum/Agent-MCP 3.0.0 dropped the SSE+messages pair in favour of
# a single `POST/GET/DELETE /mcp` endpoint. The router exposes it
# at `/agent-mcp/<name>/mcp`. Phase 6 (this file) removed the
# transitional 410-Gone handlers for `/agent-mcp/__sse/<name>` and
# `/agent-mcp/__messages/<name>/{rest}`: the old shapes now 404 via
# aiohttp's default behaviour, which is the intent — old configs
# should see a hard failure indicating the endpoint is gone.


def _w1_redirect(new_path: str) -> web.Response:
    """Build a 302 to ``new_path`` for the W1 single-tenant redirect.

    302 (not 301/308) because the wrong-name URL isn't *permanently*
    invalid — switching the operator's home-manager config back to
    multi-tenant would restore independent project URLs. Use 302
    Found so caches and clients don't pin the rewrite.

    Decision #9 (W1) in plan ``prancy-napping-pie`` locks the
    section-path-preserving shape: e.g. a request for
    ``/agent-mcp/__dashboard/foo/tasks/`` when only ``bar`` is
    configured becomes ``/agent-mcp/__dashboard/bar/tasks/``.
    """
    return web.Response(
        status=302,
        headers={"Location": new_path, "Cache-Control": "no-store"},
    )


def _maybe_single_tenant_redirect(
    req: web.Request, name: str,
) -> web.Response | None:
    """If single-tenant mode is on and ``name`` ≠ the configured project,
    return the W1 redirect response. Otherwise None (continue normally).

    The replacement substitutes only the *first* occurrence of the
    wrong name in the path, since the project segment is what the
    URL grammar guarantees uniqueness of — any later collision with
    a literal segment that happens to spell ``foo`` is coincidental
    and we don't want to touch it.
    """
    if SINGLE_TENANT_NAME is None or name == SINGLE_TENANT_NAME:
        return None
    new_path = req.path.replace(name, SINGLE_TENANT_NAME, 1)
    if req.query_string:
        new_path = f"{new_path}?{req.query_string}"
    return _w1_redirect(new_path)


async def backend_mcp_handler(req: web.Request) -> web.StreamResponse:
    """/agent-mcp/<name>/mcp → backend /mcp (Streamable HTTP transport)

    Spec rev 2025-03-26. Stateless: no Mcp-Session-Id handshake; every
    request is independent. Backend restarts are invisible to clients
    beyond the in-flight request.

    Auth modes admitted (first match wins):

      1. ``Authorization: Bearer <token>`` — forwarded verbatim to
         the backend, which validates it against ``g.active_agents``
         via :class:`AuthHeaderMiddleware`. The router does NOT
         pre-check the bearer; the backend's cache is the single
         source of truth for "is this token live right now?". See
         the inline comment below for why the router used to
         pre-check and why that's gone.
      2. ``agent_mcp_session`` cookie pointing at a live operator
         session whose user is a member of the project. retire-system-
         token Wave 2 (2026-06-23): the router signs a
         ``X-Agent-MCP-Forwarded-Operator`` header here and the
         backend's ``AuthHeaderMiddleware`` (Wave 1) verifies it
         against the per-project HMAC key. The legacy cookie→admin-
         bearer translation (PR #204 / PR #207) is gone — there is no
         system bearer to inject anymore.

    Failure modes:

      * No auth at all → 401 (router).
      * Bearer present but unknown → 401 (backend's
        ``AuthHeaderMiddleware``; router forwards unconditionally).
      * Cookie present but unknown / expired / non-member → 401
        (router; the cookie path resolves identity before forwarding
        so the operator never reaches the backend).
      * Cookie valid but the backend systemd unit refused to spawn
        (unknown project, broken unit file, spawn timeout) → 401.
        ``_forwarding_header_from_cookie`` explicitly triggers
        ``_ensure`` so the unit's ExecStartPre writes the HMAC key
        BEFORE the key is read; on a cold backend the cookie path is
        self-sufficient and does not depend on prior bearer traffic.

    The cookie path runs INSIDE this handler rather than in
    ``require_operator_session_middleware`` because ``/agent-mcp/mcp/``
    is in the middleware's unauth allow-list (the agent-side bearer
    path was the only auth scheme until Wave 2). Moving it out of the
    allow-list would gate the agent path on cookie middleware that
    doesn't apply.
    """
    name = req.match_info["name"]
    # SEC FINDING 1: anchor the pre-auth timing floor at handler entry
    # so every pre-auth 401 (unknown-project collapse, no-cred, bad
    # cookie, backend-rejected bearer) returns at the same wall-clock
    # target regardless of whether a backend round-trip happened.
    t0 = time.monotonic()
    redirect = _maybe_single_tenant_redirect(req, name)
    if redirect is not None:
        return redirect

    bearer = _extract_bearer(req)

    # ── SEC (auth-before-resolve, owner-authorised) ──────────────────
    # ``_resolve_project_or_alias`` raises 404 for an unknown project;
    # the no-credential path below returns 401. Resolving BEFORE the
    # auth gate therefore turned the pair into a project-existence
    # oracle — an anonymous ``POST /agent-mcp/mcp/<unknown>`` got 404
    # while ``<known>`` got 401, so valid project names could be
    # enumerated by status-code differencing.
    #
    # Gate on credential PRESENCE first: a caller with NEITHER a bearer
    # NOR an operator-session cookie gets a uniform 401 whether or not
    # the project exists. The router can't validate a bearer itself
    # (the backend's ``AuthHeaderMiddleware`` is the source of truth)
    # and validating the cookie needs the resolved project, so
    # presence is the strongest check we can make pre-resolve — and it
    # closes the realistic anonymous-enumeration attack.
    #
    # A junk ``Authorization: Bearer <garbage>`` clears this presence
    # gate, so the gate alone did NOT close the oracle: the resolve
    # below (which 404s an unknown project) still fired for a junk
    # bearer, leaking unknown-vs-known by status code. The resolve is
    # therefore wrapped just below to collapse its 404 into the same
    # uniform 401 — see that comment. ``resolve()`` itself is
    # intentionally NOT changed; it is shared by the operator-session-
    # gated REST/lifecycle handlers that legitimately need 404 for
    # unknown projects (and that unauthenticated callers can't reach).
    if bearer is None and not req.cookies.get("agent_mcp_session", ""):
        raise await _floored_unauthorized(t0)

    # Method whitelist — verify-all-v4 MUTATING #2 follow-up. The MCP
    # Streamable HTTP transport (spec rev 2025-03-26) defines only
    # three verbs:
    #
    #   POST   /mcp   — JSON-RPC request/response (the hot path)
    #   GET    /mcp   — long-lived SSE for server-initiated
    #                   notifications; requires a per-agent bearer
    #                   because the backend's ``_handle_get`` derives
    #                   ``agent_id`` from the bearer to fan out from
    #                   ``session_registry``. Cookie-only callers have
    #                   no derivable agent_id and would crash there as
    #                   500 ``session_registry_no_agent`` — surfaced by
    #                   verify-all-v4 MUTATING #2 wrong-HTTP-method
    #                   full-catalog probe.
    #   DELETE /mcp   — session termination (the SDK returns 405 in
    #                   stateless mode; we forward and let it decide).
    #
    # PUT/PATCH/OPTIONS/HEAD/etc. have no meaning here. Short-circuit
    # them with a clean 405 instead of letting them fall through to
    # the backend's SDK manager (which would produce an ugly 500-via-
    # internal-exception response that leaks no info but offends
    # wrong-status hygiene).
    if req.method == "GET" and bearer is None:
        # Cookie-only GET: spec verb but cookie path can't carry it.
        # Return 405 instead of proxying — saves a backend round-trip
        # AND avoids the ``session_registry_no_agent`` 500.
        raise web.HTTPMethodNotAllowed(
            method=req.method,
            allowed_methods=["POST"],
            reason="GET on /mcp requires a per-agent bearer token",
        )
    if req.method not in ("POST", "GET", "DELETE"):
        raise web.HTTPMethodNotAllowed(
            method=req.method,
            allowed_methods=["POST", "GET", "DELETE"],
            # Fixed reason phrase — never reflect the caller-supplied
            # project name into the HTTP status line (SEC4 pattern).
            reason="/mcp accepts only POST, GET, or DELETE",
        )
    # Resolve alias → real project. On this transport the router can
    # never itself confirm a caller is authenticated before forwarding
    # (a bearer is validated only by the backend's AuthHeaderMiddleware;
    # the cookie path needs the resolved project to check membership),
    # so a not-yet-authenticated caller reaching an UNKNOWN project must
    # be indistinguishable from one reaching a known-but-unauthenticated
    # project — both get a uniform 401. Collapse resolve()'s 404 into
    # that same 401 here: this closes the SEC5 project-existence oracle
    # for EVERY not-yet-authenticated caller, including a junk
    # ``Authorization: Bearer <garbage>`` that cleared the presence gate
    # above. Genuine 404 semantics for unknown projects live on the
    # operator-session-gated REST/lifecycle handlers (admin_api), which
    # unauthenticated callers can't reach, so they leak nothing.
    try:
        real_name, alias_entry = _resolve_project_or_alias(name)
    except web.HTTPNotFound:
        exc = await _floored_unauthorized(t0)
        raise exc from None

    forwarding_header: tuple[str, str] | None = None
    if bearer is None:
        # No bearer header — try the operator-session cookie. The
        # dashboard's SSE subscription drops the bearer in favour of
        # the cookie (Wave 2, cleanup/wave-2-strip-frontend-admin-token).
        forwarding_header = await _forwarding_header_from_cookie(req, real_name)
        if forwarding_header is None:
            raise await _floored_unauthorized(t0)
    # When ``bearer`` is set, the request is forwarded to the backend
    # WITHOUT a router-side validity check. The backend's
    # ``AuthHeaderMiddleware`` (see ``agent_mcp.app.main_app``) gates
    # every ``/mcp`` request against ``g.active_agents`` (the
    # in-process cache of non-terminated agent rows) — that's the
    # single source of truth for "is this bearer live right now?".
    #
    # The router used to call ``_agent_token_map`` here as an extra
    # gate, but that introduced two problems:
    #
    #   1. ``_agent_token_map`` fetches the backend's
    #      ``GET /api/tokens`` over the project UDS with NO
    #      ``Authorization`` header. PR #203 (prancy-napping-pie Wave
    #      1, 2026-06-20) added ``Depends(require_operator_session)``
    #      to that endpoint, so the router's unauthenticated probe now
    #      always 401s and the map is always empty — every per-agent
    #      bearer at ``/agent-mcp/mcp/<project>`` was rejected as
    #      "invalid or missing agent bearer token".
    #   2. Even when ``_agent_token_map`` worked, its 3-second cache
    #      kept terminated tokens admissible for up to 3 s post-
    #      revoke — the backend's ``g.active_agents`` cache (synchronous
    #      with the terminate write) is strictly fresher.
    #
    # Forwarding the bearer verbatim makes ``g.active_agents`` the
    # single source of truth and fixes both problems in one move.
    alias_info: tuple[str, str] | None = None
    if alias_entry is not None:
        # Stash on the request too so downstream observers (Phase 1c
        # telemetry, future audit log) can pick it up.
        req["resolved_via_alias"] = name
        req["resolved_project"] = real_name
        alias_info = (name, alias_entry.get("expires_at", ""))
    try:
        resp = await _proxy_to_backend(
            req, real_name, "/mcp",
            alias_info=alias_info,
            inject_header=forwarding_header,
        )
    except web.HTTPRequestEntityTooLarge:
        # SEC FINDING 2: the request body is read inside
        # ``_proxy_to_backend`` (only reached for a KNOWN project); an
        # UNKNOWN project 401s above before any body read. So an
        # oversized body 413s only for a known project — a 413-vs-401
        # project-existence oracle for a not-yet-authenticated caller.
        # Collapse the 413 into the same uniform (floored) pre-auth 401
        # so body size can't distinguish known from unknown. A genuine
        # 413 for an authenticated caller is an acceptable loss here:
        # on this transport the router can't confirm the bearer is live
        # until AFTER the body is read + forwarded, so it can't tell an
        # authenticated oversized request from an attacker's probe.
        exc = await _floored_unauthorized(t0)
        raise exc from None
    # ── SEC5 (401-envelope parity, owner-authorised) ─────────────────
    # A bearer that the backend rejects (not live in ``g.active_agents``)
    # comes back as the backend's OWN 401: a ``Server: uvicorn`` header,
    # a JSON ``invalid_bearer``/``agent_terminated`` body, NO
    # ``WWW-Authenticate``, ~230 bytes. The router's own pre-auth 401
    # (unknown project / no creds) is ``_unauthorized()``: ``Server``
    # from aiohttp, a ``WWW-Authenticate`` challenge, a plaintext reason,
    # ~42 bytes. Those five distinguishers let an anonymous caller diff
    # the two 401s and enumerate valid project names — PR #279 unified
    # the status CODE but left the ENVELOPE divergent (SEC5 still open).
    #
    # A 401 on THIS transport always means "not-yet-authenticated" (the
    # backend is the sole judge of bearer validity and we forwarded the
    # bearer to reach here). Collapse it into the router's canonical
    # ``_unauthorized()`` so a KNOWN-but-unauthenticated project and an
    # UNKNOWN project are byte-indistinguishable to that caller — same
    # status line/reason, same body, same WWW-Authenticate, and the
    # upstream ``Server: uvicorn`` fingerprint is dropped. Authenticated
    # 2xx responses (and any non-401 the backend returns) pass through
    # untouched. The backend's richer ``agent_terminated`` hint is still
    # available to a caller hitting the backend UDS directly; over the
    # public router boundary, uniform-401 hygiene wins.
    if resp.status == 401:
        raise await _floored_unauthorized(t0)
    return resp


async def _forwarding_header_from_cookie(
    req: web.Request, real_project_name: str,
) -> tuple[str, str] | None:
    """If ``req`` carries a valid operator-session cookie AND that
    operator is a member of ``real_project_name``, return a freshly-
    signed ``(header_name, header_value)`` tuple ready to forward
    upstream. Otherwise return None.

    Membership is checked via ``identity.is_project_member`` —
    sysadmin bypass intentionally NOT applied here: an operator who is
    not a project member should not be silently elevated by the
    sysadmin flag on this transport. The dashboard never lands on this
    code path for a project the operator isn't a member of (the
    project picker only shows accessible projects).

    Returns None (which the caller maps to 401) when:

      * No ``agent_mcp_session`` cookie.
      * Cookie not resolvable to a live operator session.
      * Operator is not a member of the project.
      * The per-project HMAC key isn't on disk even after ``_ensure``
        completed (vanishingly rare; would imply the unit's
        ExecStartPre didn't run or didn't write the file).

    Raises ``web.HTTPException`` (propagated as 4xx/5xx by aiohttp)
    when ``_ensure`` rejects the spawn — unknown project (404), bad
    unit file (500), spawn timeout (504). These are real upstream
    errors and must NOT be masked as a generic 401: an authenticated
    operator hitting an un-spawnable backend deserves the same
    status the bearer path would surface when ``_proxy_to_backend``
    calls ``_ensure`` itself.

    The returned tuple is fed into ``_proxy_to_backend``'s
    ``inject_header`` parameter; the backend's ``AuthHeaderMiddleware``
    verifies the HMAC against ``g.forwarding_hmac_key`` (same per-
    project key the router-side ``_po.get_forwarding_hmac_key``
    returns).

    F015 v5 (this commit): the cookie path explicitly calls
    ``_ensure`` BEFORE reading the HMAC key. F015 v4 (PR #214) moved
    key generation out of the router and into the systemd unit's
    ExecStartPre — but ExecStartPre only runs when ``systemctl
    start`` runs, and ``systemctl start`` only runs from inside
    ``_ensure``. The pre-v4 code path implicitly assumed ``_ensure``
    had already populated the key file by the time the cookie path
    ran; post-v4 that's only true if SOMETHING triggered ``_ensure``
    first. On a cold backend (no agent-side bearer traffic, dashboard
    is the first caller), nothing triggers it and every cookie
    request 401s in a tight loop. Spawning here makes the cookie
    path self-sufficient.
    """
    # Lazy imports — keep module import-time side-effect-free.
    from .login import resolve_current_user
    from . import identity
    from ..app import forwarding_header as _fh

    # Fast path: no cookie → no cookie auth. Skip both the session
    # lookup and the HMAC key probe.
    if not req.cookies.get("agent_mcp_session", ""):
        return None
    from . import group_resolver

    user = resolve_current_user(req)
    if user is None:
        return None
    try:
        if not identity.is_project_member(user["user_id"], real_project_name):
            return None
    except Exception:  # pragma: no cover - defensive
        return None
    # Resolve the operator's REAL per-project role and sign THAT — not
    # a fixed "operator". This is the SEC-1 fix: signing a fixed role
    # let a viewer-tier operator collect the full operator capability
    # bundle over the MCP wire. ``resolve_user_project_role`` is the
    # same resolver the REST middleware (``router/auth_middleware.py``)
    # already gates on, so the wire path and the /api path agree on the
    # caller's tier. A member with no resolvable role (shouldn't happen
    # given the ``is_project_member`` gate above, but defensive) is
    # denied rather than defaulted.
    try:
        role = group_resolver.resolve_user_project_role(
            user["user_id"], real_project_name
        )
    except Exception:  # pragma: no cover - defensive
        return None
    if role is None:
        return None
    # Ensure the backend systemd unit is started so its ExecStartPre
    # has run and written ``/run/agent-mcp/<name>/forwarding_hmac``.
    # ``_ensure`` is idempotent (no-op if already active); the same
    # function ``_proxy_to_backend`` calls a few frames down. We
    # invoke it here so the HMAC key file is guaranteed present
    # BEFORE ``get_forwarding_hmac_key`` reads it — F015 v5.
    #
    # ``_ensure`` raises ``web.HTTPException`` (404/500/504) on real
    # spawn failures; we DON'T swallow them. Letting them bubble
    # preserves the contract the bearer path already exposes (the
    # bearer path's ``_proxy_to_backend`` makes the same call and
    # raises the same exceptions). Masking them as 401 here would
    # be a regression — an authenticated operator on a broken
    # backend should see the real 5xx, not a misleading auth error.
    await _ensure(real_project_name, "backend")
    key = _po.get_forwarding_hmac_key(real_project_name)
    if key is None:
        # Backend spawn completed but the HMAC file still isn't on
        # disk. Pre-v5 this was the normal "haven't spawned yet"
        # case; post-v5 we've already called ``_ensure`` above, so
        # reaching this branch means the systemd unit's ExecStartPre
        # didn't write the file. That's a deployment-side bug
        # (missing/broken nix module hook); falling through to 401
        # is the correct user-visible signal.
        return None
    operator_id = str(user["user_id"])
    signed = _fh.sign(operator_id, role, key)
    return (_fh.HEADER_NAME, signed)


async def backend_api_handler(req: web.Request) -> web.StreamResponse:
    """/agent-mcp/__api/<name>/{rest} → backend /api/{rest}

    PR-A: gated by the strict Accept header
    ``application/vnd.agent-mcp.v1+json``. A request that doesn't ask
    for that media type explicitly gets a 406 with a structured error
    body (see ``_api_version_required_response``). This is a public
    breaking change for callers that previously sent no Accept header
    at all — that's intentional. CORS preflights (OPTIONS) are exempt
    because the browser sends them automatically without an Accept
    header.
    """
    if req.method != "OPTIONS":
        if not _accept_includes_strict_api_media(req.headers.get("Accept", "")):
            return _api_version_required_response()

    rest = req.match_info.get("rest", "")
    name = req.match_info["name"]
    redirect = _maybe_single_tenant_redirect(req, name)
    if redirect is not None:
        return redirect
    real_name, alias_entry = _resolve_project_or_alias(name)
    alias_info: tuple[str, str] | None = None
    if alias_entry is not None:
        req["resolved_via_alias"] = name
        req["resolved_project"] = real_name
        alias_info = (name, alias_entry.get("expires_at", ""))
    return await _proxy_to_backend(
        req, real_name, f"/api/{rest}", alias_info=alias_info,
    )


# ── Dashboard static handler ─────────────────────────────────────────
#
# retire-system-token Wave 5 (2026-06-23) removed ``_mcp_call_admin``
# (the router-side helper that opened an SSE session as Admin to seed
# tasks + create agents) along with ``router/admin_api.py``'s
# ``create_agent_handler`` that wrapped it. Both paths fetched an
# ``admin_token`` field from ``/api/tokens`` that Wave 3 removed; the
# helper was dead-on-arrival post-Wave-3 and had no other callers (the
# dashboard hits ``/api/agents`` on the per-project backend directly).


_DASHBOARD_ROOT = Path(DASHBOARD_DIR).resolve()
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".mjs":  "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".ico":  "image/x-icon",
    ".txt":  "text/plain; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _safe_dashboard_path(rest: str) -> Path | None:
    """Resolve `rest` against the dashboard root, refusing escapes.

    ``.resolve()`` raises ``ValueError: embedded null byte`` on a NUL
    in the path (R6-F3) — and ``OSError`` on other resolve-time OS
    failures. Both map to the existing "unsafe → None" path, which the
    callers turn into a clean 404 rather than an unhandled 500.
    """
    try:
        candidate = (_DASHBOARD_ROOT / rest).resolve()
    except (ValueError, OSError):
        return None
    try:
        candidate.relative_to(_DASHBOARD_ROOT)
    except ValueError:
        return None
    return candidate


def _serve_dashboard_file(
    candidate: Path, *, cache_control: str,
) -> web.Response:
    """Serve a file from the dashboard tree, running it through the
    Phase 4 sentinel substitution if the Content-Type is one of the
    eligible text types (HTML/JS/CSS).

    Binary files (images, fonts, JSON manifests) skip substitution and
    are read straight from disk — substitution could corrupt their
    bytes if a chance sequence happened to match the sentinel.

    Reads the configured prefix from module-level ``ASSET_PREFIX`` on
    each call rather than capturing it at startup, so tests
    monkey-patching ``ASSET_PREFIX`` after import see their override
    reflected immediately.
    """
    ctype = _MIME.get(candidate.suffix.lower(), "application/octet-stream")
    if _asset_prefix.content_type_needs_substitution(ctype):
        body = _asset_prefix.substitute_file_bytes(candidate, ASSET_PREFIX)
        return web.Response(
            body=body,
            headers={
                "Content-Type": ctype,
                "Cache-Control": cache_control,
            },
        )
    # Binary / structured-data file → pass through verbatim.
    raw = candidate.read_bytes()
    return web.Response(
        body=raw,
        headers={
            "Content-Type": ctype,
            "Cache-Control": cache_control,
        },
    )


async def overview_dashboard_handler(req: web.Request) -> web.StreamResponse:
    """Serve the Next.js overview page at /agent-mcp/__dashboard/.

    Phase 3.5a: the React overview lives at the bare path; the
    dashboard JS detects the missing project segment in
    `window.location.pathname` and renders the cross-project cards
    instead of the per-project dashboard.

    Implementation-wise this is a one-shot serve of the static
    export's `index.html`; routing inside the SPA owns the rest.

    Phase 4: HTML body is run through the asset-prefix sentinel
    substitution so embedded ``__AGENT_MCP_ASSET_PREFIX__/_next/…``
    URLs are rewritten to the configured runtime prefix.
    """
    candidate = _safe_dashboard_path("index.html")
    if candidate is None or not candidate.is_file():
        raise web.HTTPNotFound()
    return _serve_dashboard_file(candidate, cache_control="no-store")


async def _warm_backend(name: str) -> None:
    """Best-effort, fire-and-forget lazy-spawn of a project's backend.

    Fired from the dashboard ``/app/<name>/`` handler so opening the
    dashboard warms the backend while the static shell paints —
    consistent with how the ``/api/`` handler triggers ``_ensure`` on
    first contact. A non-browser client (curl, a smoke test, a health
    probe) fetches only the HTML and never runs the SPA JS that would
    otherwise fire the first ``/api/<name>/...`` XHR, so without this
    the backend unit would never start for such callers.

    Swallows every failure — unknown project (404), unit error (500),
    spawn timeout (504): serving the static shell must NEVER depend on
    the backend coming up (the SPA-fallback contract). Real spawn
    errors still surface on the subsequent ``/api/<name>/...`` XHR,
    which awaits ``_ensure`` and maps failures to the right status.
    """
    try:
        await _ensure(name, "backend")
    except web.HTTPException as exc:
        log.debug("dashboard warm-start for %r skipped: %s", name, exc.reason)
    except Exception:  # pragma: no cover - defensive; never break serve
        log.debug("dashboard warm-start for %r failed", name, exc_info=True)


# BL-R6-2a: names with an in-flight warm-start task. A flood of
# ``/app/<name>/`` GETs (each serving only the static shell, no JS)
# would otherwise spawn one untracked warm task per request; we dedup
# so at most one warm-start is pending per project at a time. Folded
# into ``ProjectRuntime.warm_inflight``; re-exported here as a
# set-membership view over ``runtime`` so the call sites below (and
# any ``name in router._warm_inflight`` test) keep working.
_warm_inflight = _po._warm_inflight


def _schedule_backend_warm(req: web.Request, name: str) -> None:
    """Kick a tracked, non-blocking backend warm-start for ``name``.

    Registered in the app's proxy-task set so ``_drain_proxy_tasks``
    cancels it cleanly on shutdown rather than leaking a pending task.

    BL-R6-2a dedup: skip scheduling when a warm-start for ``name`` is
    already pending, or when the unit is already known-active (a fresh
    ``last_active`` entry, kept warm by the ``/api/`` path). Both keep
    a burst of shell-only GETs from accumulating redundant tasks — the
    underlying ``_ensure`` is idempotent, but the task churn isn't free.
    """
    if name in _warm_inflight:
        return
    if (name, "backend") in last_active:
        return
    _warm_inflight.add(name)
    task = asyncio.create_task(_warm_backend(name))
    tasks = _proxy_task_set(req.app)
    tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _warm_inflight.discard(name)
        tasks.discard(t)

    task.add_done_callback(_done)


async def dashboard_handler(req: web.Request) -> web.StreamResponse:
    """Serve the Next.js page HTML at /agent-mcp/__dashboard/<name>/.

    The HTML's embedded `<script src=…>` URLs come pre-prefixed with
    the build-time sentinel ``__AGENT_MCP_ASSET_PREFIX__``; Phase 4's
    `_serve_dashboard_file` substitutes the configured runtime prefix
    (default ``/agent-mcp/__dashboard``) before the bytes go on the
    wire. Assets themselves are served by
    `dashboard_assets_handler` below.

    Opening a per-project dashboard also warm-starts that project's
    backend (best-effort, non-blocking — see ``_warm_backend``) so it's
    ready by the time the SPA fires its first ``/api/<name>/...`` XHR,
    and so non-browser callers that never run the JS still trigger the
    documented "lazily on first request" spawn.
    """
    name = req.match_info.get("name", "")
    if name:
        redirect = _maybe_single_tenant_redirect(req, name)
        if redirect is not None:
            return redirect
        # SC-R6-1: only warm-start the backend for an AUTHORIZED
        # caller. The auth middleware serves this same SPA shell to
        # authenticated NON-MEMBERS (to avoid a project-existence
        # oracle, round-4), so gating the spawn on the middleware's
        # ``_warm_authorized`` flag — set only on the sysadmin,
        # sufficient-membership, and single-tenant branches — stops any
        # authenticated operator from activating an arbitrary tenant's
        # backend via a plain ``GET /app/<victim>/``. The response
        # stays a uniform 200 shell for member / non-member / bogus
        # slug alike; only the side-effect is authorization-gated.
        if req.get("_warm_authorized"):
            _schedule_backend_warm(req, name)
    rest = req.match_info.get("rest", "")
    if rest == "" or rest.endswith("/"):
        candidate = _safe_dashboard_path(rest + "index.html")
    else:
        candidate = _safe_dashboard_path(rest)
    if candidate is None or not candidate.is_file():
        if candidate is not None:
            alt = candidate.with_suffix(".html")
            if alt.is_file() and _safe_dashboard_path(alt.name) is not None:
                candidate = alt
            else:
                # SPA fallback: a path like `/tasks` from PR #76's URL
                # routing has no matching file in the static export, but
                # the client-side React router knows how to render it.
                # Serve the root index.html and let the SPA take over.
                candidate = _safe_dashboard_path("index.html")
                if candidate is None or not candidate.is_file():
                    raise web.HTTPNotFound()
        else:
            raise web.HTTPNotFound()
    # HTML may change between rebuilds (different chunk hashes embedded
    # in <script> tags). Force fresh fetches so a redeploy doesn't get
    # masked by the browser disk cache.
    return _serve_dashboard_file(candidate, cache_control="no-store")


async def dashboard_assets_handler(req: web.Request) -> web.StreamResponse:
    """Serve Next.js static assets at /agent-mcp/assets/<rest> (PR-B).

    PR-B moved the assets prefix to a top-level segment
    (/agent-mcp/assets/<rest>) so the asset bundle is decoupled from
    the dashboard pages path. The on-disk layout is unchanged: every
    asset still lives under DASHBOARD_DIR/_next/... as Next.js emits
    it. The sentinel substitution rewrites the build-time
    ``__AGENT_MCP_ASSET_PREFIX__`` to the configured ASSET_PREFIX
    (default /agent-mcp/assets), and Next.js's webpack runtime
    appends its own ``/_next/static/...`` segments on top of that —
    so the resulting public URLs are /agent-mcp/assets/_next/... .
    The route's `{rest:.*}` captures the `_next/...` tail and the
    handler resolves it against the dashboard dir as-is.

    Binary files (fonts, images) pass through unchanged; substitution
    only touches HTML/JS/CSS Content-Types.
    """
    rest = req.match_info.get("rest", "")
    candidate = _safe_dashboard_path(rest)
    if candidate is None or not candidate.is_file():
        raise web.HTTPNotFound()
    # Next.js content-hashes every chunk filename; the same URL is
    # guaranteed to map to the same bytes forever (a different prefix
    # → different bytes, but content hashes never re-hit so cache
    # poisoning across prefixes is impossible). Mark immutable so the
    # browser skips conditional revalidation on reload.
    return _serve_dashboard_file(
        candidate,
        cache_control="public, max-age=31536000, immutable",
    )


# ── Idle reaper + alias reaper + startup reconciliation ─────────────
# All three live in ``project_orchestrator`` (PR-C). Re-exported here
# so existing call sites (the on_startup hooks below, tests that
# monkeypatch ``router_module.reaper`` / ``router_module._alias_reaper_tick``)
# keep working without churn.
reaper = _po.reaper
alias_reaper = _po.alias_reaper
_alias_reaper_tick = _po._alias_reaper_tick
reconcile_on_startup = _po.reconcile_on_startup
_ALIAS_REAPER_INTERVAL_SEC = _po._ALIAS_REAPER_INTERVAL_SEC


async def shutdown(app: web.Application) -> None:
    """No-op: backends are systemd-supervised and outlive the router."""
    return


# ── Graceful proxy drain (router-graceful-shutdown) ────────────────
#
# An in-flight `_proxy_to_backend` task can be open for minutes — the
# MCP Streamable-HTTP dashboard channel sits inside one `await
# up.read()` while the backend trickles SSE events. On SIGTERM,
# aiohttp's runner asks `Application.shutdown()` to run the
# `on_shutdown` callbacks, then waits up to `shutdown_timeout`
# (default 60 s) for in-flight handlers to finish. Without
# intervention the proxy task never sees the signal: it's blocked on
# I/O, not awaiting a cancellation point we control.
#
# Fix: track every proxy task in `app["_proxy_tasks"]`. The
# `_drain_proxy_tasks` shutdown hook cancels each one, which
# propagates through aiohttp's ClientSession context and tears down
# the upstream UDS read. Combined with the `shutdown_timeout=3.0` on
# `web.run_app` (see `main()` / `cli.router_cmd`), this caps the
# router's SIGTERM-to-exit window well inside systemd's
# `TimeoutStopSec=15s` (defense-in-depth) — the operator-visible
# "Stopping … Stopped" gap drops from 90 s to ~3 s.
PROXY_TASKS_KEY = "_proxy_tasks"


def _proxy_task_set(app: web.Application) -> set[asyncio.Task]:
    """Return the per-app in-flight proxy-task tracking set.

    Allocated eagerly in ``make_app``; the ``.setdefault`` here is
    a belt-and-braces guard for callers (typically tests) that
    construct an aiohttp Application directly without going through
    ``make_app``.
    """
    return app.setdefault(PROXY_TASKS_KEY, set())


async def _drain_proxy_tasks(app: web.Application) -> None:
    """``on_shutdown`` hook: cancel every in-flight `_proxy_to_backend`.

    Logs the count for post-mortem visibility but does not swallow
    cancellation errors — propagating them keeps aiohttp's runner
    informed that handlers were torn down cleanly rather than timed
    out, which is the difference between an ordered shutdown and a
    SIGKILL on the next restart.
    """
    tasks = list(app.get(PROXY_TASKS_KEY, ()))
    if not tasks:
        return
    log.info(
        "router shutdown: cancelling %d in-flight proxy task(s)",
        len(tasks),
    )
    for t in tasks:
        if not t.done():
            t.cancel()
    # Wait briefly for the cancellations to propagate. Anything still
    # hanging after this falls through to aiohttp's `shutdown_timeout`
    # window, which we keep short on purpose.
    await asyncio.gather(*tasks, return_exceptions=True)


# ── Overview support (Phase 3.5a) ───────────────────────────────────
# Project list + overview handlers themselves live in
# ``agent_mcp.router.admin_api`` (ADR 0014) and call into the
# helpers below.
#
# Backs the new React route `/__dashboard/` (the dashboard overview).
# One JSON envelope per request — three small COUNTs per project on a
# warm SQLite + one `systemctl is-active` per project. We cache the
# envelope for `_OVERVIEW_CACHE_TTL_SEC` so the dashboard's fan-out of
# parallel API calls during first paint doesn't hammer systemd-userd
# or stat() the SQLite file dozens of times. Cache invalidation on
# project mutation happens organically — the TTL is short enough
# (3s) that the staleness window is invisible to a human eye.


_OVERVIEW_CACHE_TTL_SEC: float = 3.0
_overview_cache: tuple[float, dict] | None = None


def _project_db_path(workspace: str) -> Path:
    """Per-project SQLite lives at ``<workspace>/.agent/mcp_state.db``."""
    return Path(workspace) / ".agent" / "mcp_state.db"


def _project_counts(workspace: str) -> dict[str, int]:
    """Three COUNT queries against the project's SQLite. Returns
    zeros (and never raises) when the DB file or any table is missing
    — a freshly-registered project that hasn't been touched yet has
    no DB, and we'd rather render `0` than blank a card."""
    db = _project_db_path(workspace)
    out = {"agents": 0, "tasks": 0, "open_messages": 0}
    if not db.is_file():
        return out
    try:
        # `mode=ro` + `uri=True` prevents accidental writes (and lets
        # us open a DB that's currently held by the backend without
        # contention beyond the lock the backend holds at write time).
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            cur = con.cursor()
            for table, key in (
                ("agents", "agents"),
                ("tasks", "tasks"),
            ):
                try:
                    row = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    out[key] = int(row[0]) if row and row[0] is not None else 0
                except sqlite3.Error:
                    # Table missing in a half-migrated DB — keep 0.
                    pass
            try:
                row = cur.execute(
                    "SELECT COUNT(*) FROM agent_messages WHERE read = 0"
                ).fetchone()
                out["open_messages"] = (
                    int(row[0]) if row and row[0] is not None else 0
                )
            except sqlite3.Error:
                pass
        finally:
            con.close()
    except Exception:
        # Any unexpected DB error → render zeros, don't 500 the card.
        log.exception("overview: COUNT query failed for %s", db)
    return out


def _derive_status(
    name: str, *, running: bool, last_activity_ts: float | None, now: float,
) -> str:
    """S2: collapse (systemd state, last-activity bucket) to a
    dashboard chip enum: active/idle/sleeping/stopped/starting/failed.

    Mirrors the cutoffs the idle reaper uses (5 min for "fresh",
    4 h for "sleeping → reaped"):

      * not running                          → ``stopped``
      * running, no activity timestamp yet   → ``starting``
      * running, activity ≤ 5 min ago        → ``active``
      * running, activity ≤ 4 h ago          → ``idle``
      * running, activity > 4 h ago          → ``sleeping``

    ``failed`` is reserved for a future enhancement (systemd-side
    failure detection); we don't synthesize it from any input today.
    """
    if not running:
        return "stopped"
    if last_activity_ts is None:
        return "starting"
    age = now - last_activity_ts
    if age <= 5 * 60:
        return "active"
    if age <= 4 * 60 * 60:
        return "idle"
    return "sleeping"


def _workspace_label(workspace: str) -> str:
    """Operator-facing, non-disclosing label for a project's workspace.

    SEC (owner-authorised, defensive): the overview envelope must not
    echo the server's ABSOLUTE workspace path — that discloses the
    deployment's filesystem layout (and, under the default
    ``~/.local/share/...`` parent, the service account's home dir).

    When the workspace lives under ``DEFAULT_WORKSPACE_PARENT`` (the
    managed default) we return the path relative to that parent — for
    the common ``<parent>/<name>`` layout that's just the project name.
    For a workspace pointed somewhere custom (outside the managed root)
    we fall back to the final path component, so the directory is still
    identifiable without leaking the parent chain.
    """
    try:
        p = Path(workspace)
        try:
            return str(p.resolve().relative_to(DEFAULT_WORKSPACE_PARENT.resolve()))
        except ValueError:
            # Outside the managed parent — surface only the leaf name.
            return p.name
    except Exception:  # pragma: no cover - defensive
        return Path(workspace).name if workspace else ""


def _build_overview_envelope() -> dict:
    """Assemble the overview JSON envelope from registry + systemd +
    per-project SQLite. Cheap-ish; cached in `_overview_cache`."""
    now = time.time()
    projects_out: list[dict] = []
    for row in _REGISTRY.list():
        name = row["name"]
        workspace = row["workspace"]
        unit = _unit_name(name, "backend")
        running = _po._is_active(unit)
        ts = last_active.get((name, "backend"))
        counts = _project_counts(workspace)
        projects_out.append(
            {
                "name": name,
                # Relative label, never the absolute server path (SEC).
                "workspace": _workspace_label(workspace),
                "status": _derive_status(
                    name,
                    running=running,
                    last_activity_ts=ts,
                    now=now,
                ),
                "last_activity_ts": ts,
                "agents": counts["agents"],
                "tasks": counts["tasks"],
                "open_messages": counts["open_messages"],
                "alias": list(row.get("aliases", []) or []),
            }
        )
    envelope: dict = {
        "projects": projects_out,
        "multi_tenant": SINGLE_TENANT_NAME is None,
    }
    if SINGLE_TENANT_NAME is not None:
        envelope["single_tenant_name"] = SINGLE_TENANT_NAME
    return envelope


# The handlers for ``alias-usage``, ``remove-alias`` and ``overview``
# live in ``agent_mcp.router.admin_api`` (ADR 0014). They reuse
# ``_build_overview_envelope``, ``_project_db_path`` and the
# ``_OVERVIEW_CACHE_TTL_SEC`` cache state defined above.


def _visible_project_names(req: web.Request, names) -> set[str]:
    """Subset of ``names`` the operator behind ``req`` may see.

    SEC (owner-authorised, defensive) FINDING 4 [MED] — the router
    ``overview`` + ``projects`` listings were session-only with NO
    membership filter, so any authenticated operator (even a non-member,
    non-sysadmin) enumerated EVERY tenant's project names / stats /
    aliases. Filter to the caller's ``project_membership`` (direct OR
    via a group — same access model ``resolve_user_project_role`` uses
    for the per-project middleware, so a group-granted member keeps
    their overview). A sysadmin sees all. Same class as the {name}-route
    gate (#283 / FINDING 1); the listing routes are the collection
    analogue.

    ``req["user"]`` / ``req["is_sysadmin"]`` are populated by
    ``require_operator_session_middleware`` before these handlers run.
    """
    names = list(names)
    # Single-tenant mode is a single operator-owned box (ADR-0008): the
    # session middleware bypasses gating entirely there, so ``req`` has
    # no ``user`` / ``is_sysadmin`` stashed and there is no cross-tenant
    # audience to filter against. See everything.
    if bypasses_operator_gate():
        return set(names)
    if req.get("is_sysadmin"):
        return set(names)
    user = req.get("user") or {}
    user_id = user.get("user_id")
    if not user_id:
        return set()
    from . import group_resolver

    visible: set[str] = set()
    for name in names:
        try:
            if group_resolver.resolve_user_project_role(user_id, name) is not None:
                visible.add(name)
        except Exception:  # pragma: no cover - defensive
            # router.db not migrated / transient DB error → fail closed
            # for this project (omit it) rather than over-disclose.
            continue
    return visible


# ── Shared envelope / gate helpers ─────────────────────────────────
#
# The handlers themselves moved to ``agent_mcp.router.admin_api``
# (ADR 0014); these helpers stay here because they're imported from
# that module and from the per-project REST routes in
# ``agent_mcp/app/routes.py``.
#
# Unified error envelope (audit §2.5 — picked the
# _dispatch_through_tool shape since it already has the most adoption):
#   {"success": false, "error": "<short_code>", "message": "<human>"}
# Success uses {"success": true, ...resource_fields}.


_ERROR_INVALID_NAME = "invalid_name"
_ERROR_ALREADY_REGISTERED = "already_registered"
_ERROR_NOT_REGISTERED = "not_registered"
_ERROR_ACTIVE_SESSIONS = "active_sessions"
_ERROR_NAME_TAKEN = "name_taken"
_ERROR_ALIAS_COLLISION = "alias_collision"
_ERROR_INTERNAL = "internal_error"


def _error_envelope(
    *, error: str, message: str, status: int, extra: dict | None = None,
) -> web.Response:
    """Emit the unified error envelope used by all /api/projects handlers."""
    body: dict = {"success": False, "error": error, "message": message}
    if extra:
        body.update(extra)
    return web.json_response(body, status=status, headers={"Cache-Control": "no-store"})


def _success_envelope(payload: dict, *, status: int = 200) -> web.Response:
    """Emit the unified success envelope. `payload` is merged in
    alongside `success: true` — fields like `project`, `renamed`,
    `unregistered` come from the per-endpoint contract."""
    body: dict = {"success": True}
    body.update(payload)
    return web.json_response(body, status=status, headers={"Cache-Control": "no-store"})


async def _parse_json_body(req: web.Request) -> dict:
    """Tolerant JSON body parser. Empty body → empty dict (some POSTs
    take no fields). Non-JSON body raises 400 via the envelope."""
    raw = await req.read()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # Broaden to ValueError (was JSONDecodeError): an invalid-UTF8
        # body makes ``json.loads(bytes)`` raise ``UnicodeDecodeError``
        # — a ``ValueError`` subclass but NOT a ``JSONDecodeError`` — so
        # the narrower guard let it propagate to an uncaught 500
        # (PF-R21-1). ``ValueError`` covers BOTH JSONDecodeError and
        # UnicodeDecodeError; ``RecursionError`` (a ``RuntimeError``, so
        # not a ValueError subclass) stays explicit for the deeply-nested
        # body case (PF-R20-1). All are a malformed body → clean 400.
        # Raise via aiohttp so the per-endpoint handler can catch and
        # surface through the envelope (sticking the message into the
        # exception keeps the call sites simple).
        msg = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise web.HTTPBadRequest(
            text=json.dumps({
                "success": False, "error": "invalid_json",
                "message": f"request body is not valid JSON: {msg}",
            }),
            content_type="application/json",
        )
    if not isinstance(parsed, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({
                "success": False, "error": "invalid_json",
                "message": "request body must be a JSON object",
            }),
            content_type="application/json",
        )
    return parsed


def _rest_gated(handler):
    """Wrap a /api/projects handler with the PR-A Accept-header gate.

    The backend-proxy handler (``backend_api_handler``) inlines the
    same gate; the lifecycle handlers don't go through the proxy so
    they need the gate applied at the route level. OPTIONS preflights
    are exempt so CORS still works.
    """
    async def wrapped(req: web.Request) -> web.Response:
        if req.method != "OPTIONS":
            if not _accept_includes_strict_api_media(req.headers.get("Accept", "")):
                return _api_version_required_response()
        return await handler(req)
    return wrapped


# The PR-C ``/api/projects/...`` handlers moved to
# ``agent_mcp.router.admin_api`` under ``/api/router/projects/...``
# (ADR 0014). The single reserved top-level segment ``router``
# replaces the per-route reservation of ``projects`` in the
# middleware's ``_NON_PROJECT_API_SEGMENTS``.




# The legacy ``__create`` / ``__stop`` / ``__rename`` form-encoded
# handlers were deleted in PR (ADR 0014). REST-shaped
# equivalents live in ``agent_mcp.router.admin_api`` at
#   POST  /api/router/projects                  (create)
#   POST  /api/router/projects/<name>/stop      (stop)
#   PATCH /api/router/projects/<name>           (rename)


def _is_within_default_workspace(workspace_path: Path) -> bool:
    """Defence in depth for the D4 two-tier remove (Phase 3.5b):
    only allow a hard rm when the workspace path is rooted inside
    ``DEFAULT_WORKSPACE_PARENT``. A malicious projects.local.json edit
    listing ``/`` or ``/home/dennis`` would otherwise turn the
    dashboard's 'Also delete workspace files' checkbox into a wipe
    button. We compare on resolved paths so symlink traversal still
    hits the bound.
    """
    try:
        workspace_resolved = workspace_path.resolve()
        parent_resolved = DEFAULT_WORKSPACE_PARENT.resolve()
    except (ValueError, OSError):
        # R6-F3 class-completion: ``.resolve()`` raises ValueError on an
        # embedded-null-byte path, not just OSError. Fail closed (deny the
        # hard rm) rather than let it propagate to a 500. Latent today (the
        # workspace path is server-side), but completes the fail-closed class.
        return False
    try:
        workspace_resolved.relative_to(parent_resolved)
    except ValueError:
        return False
    return True


# The legacy ``__unregister`` form-handler was deleted in PR
# (ADR 0014). The REST-shaped equivalent is
# ``admin_api.delete_project_handler`` at
# ``DELETE /agent-mcp/api/router/projects/<name>``.


# ── Wiring helpers (client-config / installer) ───────────────────────


def _mcp_url_for(name: str) -> str:
    """Public URL of the project's Streamable HTTP /mcp endpoint.

    PR-D Shape-3 move: from /agent-mcp/<name>/mcp to /agent-mcp/mcp/<name>.
    This URL is what /__client-config and /__client-installer bake into
    the .mcp.json file operators download — so this helper is the
    one-line change that propagates the new shape to every wiring
    surface.
    """
    return f"{EXTERNAL_URL}/agent-mcp/mcp/{name}"


def _mcp_json_for(name: str, *, token: str | None = None) -> dict:
    """`.mcp.json` entry that points an MCP client at this project.

    type=http for the Streamable HTTP transport (MCP spec rev
    2025-03-26). The old type=sse + paired /messages/ endpoint were
    retired in dvaerum/Agent-MCP 3.0.0.
    """
    entry: dict = {"type": "http", "url": _mcp_url_for(name)}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return {"mcpServers": {"agent-mcp": entry}}


def _installer_script_for(name: str, *, token: str | None = None) -> str:
    """Render the installer template with `name`'s SSE URL + token.

    The template lives at $AGENT_MCP_INSTALLER_TEMPLATE
    (./installer.sh.in in the source tree). The router reads it
    once at startup and substitutes both placeholders per request.
    Passing `token=None` substitutes the literal placeholder
    string so the user knows to edit it before running.
    """
    return (
        _INSTALLER_TEMPLATE
        .replace("__AGENT_MCP_MCP_URL__", _mcp_url_for(name))
        .replace(
            "__AGENT_MCP_AGENT_TOKEN__",
            token if token else "REPLACE_WITH_YOUR_AGENT_TOKEN",
        )
    )


async def _resolve_agent_token(
    name: str, agent_id: str | None
) -> tuple[str | None, str | None]:
    """Look up (token, agent_id) for ?agent=<id> on wiring endpoints.

    Returns (None, None) when no agent is requested. Raises 404 if
    the requested agent doesn't exist. Default `agent_id` is Admin.
    """
    if agent_id is None or agent_id == "":
        agent_id = "Admin"
    tokens = await _agent_token_map(name)
    for tok, aid in tokens.items():
        if aid == agent_id:
            return tok, aid
    # Fixed reason phrase — never reflect the caller-supplied agent_id
    # or project name into the HTTP status line (SEC4 pattern).
    raise web.HTTPNotFound(reason="unknown agent")


# Wiring helpers (client-config / installer / create-agent) live
# in ``agent_mcp.router.admin_api`` (ADR 0014); they reuse
# ``_mcp_url_for``, ``_mcp_json_for``, ``_installer_script_for``
# and ``_resolve_agent_token`` defined above.


# ── Index handler ────────────────────────────────────────────────────
# The legacy server-rendered HTML index page (the original
# ``index_handler`` and its ``_list_view`` / ``_INDEX_STYLE`` /
# ``_wiring_help_panel`` / ``_create_form`` helpers) was deleted in
# Phase 6 of the router-upstream plan (prancy-napping-pie). The bare
# ``/agent-mcp/`` URL has been a 302 to the React dashboard since Phase
# 3.5a (ADR-0009); the HTML page was only retained "for reference"
# while we shipped the React equivalents of its wiring + create-project
# surfaces. Those equivalents shipped in Phase 3.5b/c, so the legacy
# HTML is now dead code.
#
# PR-A turned the bare URL into an Accept-negotiated handler: browsers
# (Accept: text/html) keep the 302; non-browser clients (everything
# else) get a JSON service descriptor instead. See ``_service_descriptor``
# and the API-versioning section higher in this file.

def _service_descriptor() -> dict:
    """Build the JSON service-descriptor body.

    PR-B: endpoint URLs now reflect the Shape-3 surface — the four
    top-level prefixes (``api``, ``app``, ``assets``, ``mcp``). A
    client that follows the descriptor links lands on the new URLs.

    The MCP transport URL stays project-suffixed (``/agent-mcp/<name>/mcp``)
    in PR-B; PR-D moves it to ``/agent-mcp/mcp/<name>``. The descriptor
    publishes the parent prefix only.
    """
    # SEC (owner-authorised, defensive): the internal package version is
    # deliberately NOT echoed here. Operators don't consume it (the
    # dashboard hard-codes its own product string), so it's pure
    # attacker-useful fingerprinting of the deployed build.
    return {
        "service": "agent-mcp",
        "mode": "single-tenant" if SINGLE_TENANT_NAME is not None else "multi-tenant",
        "endpoints": {
            "api": "/agent-mcp/api",
            "app": "/agent-mcp/app",
            "assets": "/agent-mcp/assets",
            # PR-D folded the MCP transport into a top-level /mcp/
            # prefix; clients append /<name> to reach a given project's
            # transport.
            "mcp": "/agent-mcp/mcp",
        },
        "projects_url": "/agent-mcp/api/router/projects",
        "overview_url": "/agent-mcp/api/router/overview",
        "health_url": "/agent-mcp/api/router/health",
        "single_tenant_project": SINGLE_TENANT_NAME,
    }


# v5.0.0: ``_make_mcp_url_redirect`` and ``_make_rename_redirect``
# helpers were deleted alongside the legacy 308 routes they powered.
# The 30-day grace window for /__dashboard/, /__api/<name>/<rest>, and
# /<name>/mcp shapes has expired; those URLs now 404.


async def index_handler(req: web.Request) -> web.Response:
    """GET /agent-mcp/ — Accept-negotiated descriptor / dashboard.

    PR-A (locked design, /grill-me):
      Accept: text/html       → 302 to /agent-mcp/__dashboard/
                                 (or to /__dashboard/<name>/ in
                                 single-tenant mode — same destination
                                 the pre-PR-A redirect targeted)
      anything else / unset   → 200 JSON service descriptor

    The HTML index page was deleted in Phase 6; the redirect path here
    preserves the existing browser UX while letting non-browser
    clients fetch a discovery document instead of a 302 they then have
    to parse.
    """
    if _accept_prefers_html(req.headers.get("Accept", "")):
        if SINGLE_TENANT_NAME is not None:
            target = f"/agent-mcp/app/{quote(SINGLE_TENANT_NAME)}/"
        else:
            target = "/agent-mcp/app/"
        raise web.HTTPFound(location=target)
    return web.json_response(_service_descriptor(), headers={"Cache-Control": "no-store"})


# ── Wire-up ──────────────────────────────────────────────────────────


async def _start_reaper_task(app: web.Application) -> None:
    asyncio.create_task(reaper(app))


async def _start_alias_reaper_task(app: web.Application) -> None:
    """Spawn the alias reaper coroutine on app startup. Mirrors the
    pattern used by `_start_reaper_task` for the idle-backend reaper."""
    asyncio.create_task(alias_reaper(app))


async def _init_router_identity_on_startup(app: web.Application) -> None:
    """Run router-identity migrations + env-var bootstrap at startup.

    Delegates to `agent_mcp.router.identity.init_router_db`, which:

      1. Runs the router-level Alembic migrations against router.db
         (creates the users / sessions / project_membership tables on
         a fresh deploy; no-op on already-migrated DBs).
      2. If `AGENT_MCP_BOOTSTRAP_USERNAME` and `_PASSWORD` are set
         AND the users table is empty, creates the first operator
         from them — and then strips both env vars from os.environ
         so they don't leak into agent subprocess spawns.

    The call is sync (SQLite is fine on the event loop here — it's
    a one-shot at startup, not a hot path). We import lazily to keep
    argon2-cffi's CFFI bindings off the cold-import critical path
    for any module that pulls `agent_mcp.router.app`.
    """
    del app  # signature required by aiohttp's on_startup contract.
    from .identity import init_router_db

    init_router_db()


# ── Fail-closed startup guards (deployment-contract enforcement) ────


def _resolve_bind_host() -> str:
    """Return the exact host string handed to ``web.run_app``.

    This is the ONE place that turns ``AGENT_MCP_ROUTER_HOST`` (default
    ``127.0.0.1``) into the bind host. BOTH the fail-closed startup
    guard (``_assert_startup_safe`` → ``_host_is_loopback``) AND every
    runtime entrypoint (``main()`` here, ``cli.router_cmd``) call this,
    so the string the guard *classifies* is byte-for-byte the string
    the runtime *binds* — they can never diverge.

    Whitespace is stripped so a present-but-empty / whitespace-only env
    value (``Environment=AGENT_MCP_ROUTER_HOST=`` in a unit file, or
    ``docker -e AGENT_MCP_ROUTER_HOST=``) collapses to ``""`` — the
    same string aiohttp/asyncio bind to ALL interfaces. Classification
    (below) treats that as non-loopback, so the single-tenant guard
    fails closed rather than silently publishing an auth-disabled
    dashboard. We do NOT rewrite ``""`` → ``127.0.0.1``: the "empty =
    bind all" idiom stays valid for multi-tenant, which HAS the auth
    gate.
    """
    return os.environ.get("AGENT_MCP_ROUTER_HOST", "127.0.0.1").strip()


def _host_is_loopback(host: str) -> bool:
    """True iff ``host`` binds only the loopback interface (or a UDS).

    A unix-socket path (``unix:...`` or an absolute path) is treated as
    loopback-equivalent — it isn't a network listener. A hostname that
    isn't a bare IP is resolved conservatively: only the literal
    ``localhost`` counts.

    An empty / whitespace-only host is NOT loopback: aiohttp/asyncio
    bind ``""`` to every interface (``0.0.0.0`` + ``::``), exactly like
    an explicit ``0.0.0.0``. Classifying it as loopback is the R6-F1
    hole — the guard would pass while the runtime published all
    interfaces. Fail closed instead.
    """
    host = host.strip()
    if not host:
        return False
    if host.startswith("unix:") or host.startswith("/"):
        return True
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not a bare IP and not a known loopback name — treat as a
        # network bind (fail-closed: better to refuse than to assume
        # a hostname is loopback).
        return False


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _assert_startup_safe(single_tenant_name: str | None) -> None:
    """Refuse to start on an unsafe bind + mode combination.

    Two landmines the internet-hardening audit flagged:

      1. Single-tenant mode (ADR-0008) disables ALL operator-session
         auth under ``/agent-mcp`` — the whole surface is open. That is
         safe ONLY on a loopback / UDS bind fronted by a trusted proxy.
         Binding a public interface in single-tenant mode publishes an
         unauthenticated admin dashboard, so we refuse to start.

         Escape hatch for known-isolated non-loopback binds (e.g. a
         qemu user-mode guest where ``0.0.0.0`` is reachable only via
         host port-forwarding): ``AGENT_MCP_ALLOW_INSECURE_BIND=1``.

      2. Secure-cookie enforcement. When
         ``AGENT_MCP_REQUIRE_SECURE_COOKIES`` is set the cookie helpers
         fail closed (always ``Secure``). If instead the bind is
         non-loopback with NEITHER that flag NOR an HTTPS external URL,
         we emit a loud warning (not a refusal — plain-HTTP behind a
         TLS-terminating proxy is a legitimate shape).
    """
    host = _resolve_bind_host()
    loopback = _host_is_loopback(host)
    allow_insecure = _env_truthy(
        os.environ.get("AGENT_MCP_ALLOW_INSECURE_BIND")
    )

    if single_tenant_name is not None and not loopback and not allow_insecure:
        raise RuntimeError(
            "Refusing to start: single-tenant mode disables operator "
            f"authentication, but the router is binding a non-loopback "
            f"host ({host!r}). This would publish an unauthenticated "
            "admin dashboard to the network. Fix one of:\n"
            "  * bind loopback (unset AGENT_MCP_ROUTER_HOST or set it "
            "to 127.0.0.1) and front the router with a trusted "
            "reverse proxy, OR\n"
            "  * run in multi-tenant mode (drop --single-tenant) so "
            "the operator-session gate is enforced, OR\n"
            "  * if this bind is genuinely isolated (e.g. a qemu guest "
            "reachable only via host port-forwarding), set "
            "AGENT_MCP_ALLOW_INSECURE_BIND=1 to acknowledge the risk."
        )

    if not loopback:
        require_secure = _env_truthy(
            os.environ.get("AGENT_MCP_REQUIRE_SECURE_COOKIES")
        )
        external_url = os.environ.get("AGENT_MCP_EXTERNAL_URL", "")
        https_signal = external_url.lower().startswith("https://")
        if not require_secure and not https_signal:
            log.warning(
                "Router is binding a non-loopback host (%r) with no TLS "
                "signal: AGENT_MCP_REQUIRE_SECURE_COOKIES is unset and "
                "AGENT_MCP_EXTERNAL_URL is not https. Session cookies "
                "will be set WITHOUT the Secure flag. If this deploy is "
                "internet-facing, terminate TLS upstream and set "
                "AGENT_MCP_REQUIRE_SECURE_COOKIES=1.",
                host,
            )


def make_app(
    *,
    single_tenant_name: str | None = None,
    single_tenant_workspace: str | None = None,
) -> web.Application:
    """Build the aiohttp Application.

    When ``single_tenant_name`` is set, the router runs in N=1 mode
    (Phase 3 / ADR-0008). The route table is unchanged — same URL
    surface in both modes (decision #1) — but module-level
    ``SINGLE_TENANT_NAME`` is populated so the disabled-write
    endpoints and W1 redirect short-circuits kick in for individual
    handlers.

    ``single_tenant_workspace`` is informational at the router level
    (the ExecStartPre seed step in the home-manager module does the
    registry write); we still accept it so the CLI can pass it
    straight through without re-deriving the registry write here.
    """
    global SINGLE_TENANT_NAME, SINGLE_TENANT_WORKSPACE
    SINGLE_TENANT_NAME = single_tenant_name
    SINGLE_TENANT_WORKSPACE = single_tenant_workspace

    # Wire-in: Phase 1 PR C of prancy-napping-pie (operator login).
    #
    # ``register_setup_routes`` appends the empty-users redirect
    # middleware; aiohttp requires middlewares to be present on the
    # Application AT CONSTRUCTION TIME (the middleware chain is frozen
    # once the app starts), so we must pass ``middlewares=`` to the
    # constructor rather than mutating ``app.middlewares`` post-hoc.
    # We import lazily to keep the Jinja Environment off any
    # ``agent_mcp.router.app`` import that doesn't go through
    # ``make_app`` (e.g. a future ``router_module.do_thing()`` call
    # path used only by tests).
    from .setup_wizard import empty_users_redirect_middleware
    from .login import register_login_routes
    from .setup_wizard import register_setup_routes
    from .auth_middleware import require_operator_session_middleware
    from .security_headers import security_headers_middleware
    from . import rate_limit

    # Fail-closed deployment-contract guards. These raise at
    # construction time (which aborts startup) when the resolved bind
    # host + mode combination would expose an auth-disabled surface to
    # the network. See ``_assert_startup_safe``.
    _assert_startup_safe(single_tenant_name)

    # Middleware order matters (outermost first):
    #   1. security-headers — wraps everything so even 429/401/redirect
    #      responses carry the headers.
    #   2. rate-limit — reject floods BEFORE the argon2 verify / auth
    #      work runs.
    #   3. empty-users-redirect — a fresh deploy with no operator
    #      account 303s to /setup before the session gate.
    #   4. session gate — the operator-session enforcement.
    app = web.Application(
        middlewares=[
            security_headers_middleware,
            rate_limit.rate_limit_middleware,
            empty_users_redirect_middleware,
            require_operator_session_middleware,
        ],
        # SEC FINDING 2: make the request-body cap explicit (aiohttp's
        # default is a silent 1 MiB). ``backend_mcp_handler`` collapses
        # the resulting 413 into the uniform pre-auth 401.
        client_max_size=_MCP_MAX_BODY_BYTES,
    )
    # Load rate-limit config + limiter instances onto the app. The
    # middleware (in the list above) reads them at request time.
    _rl_cfg = rate_limit.attach(app)
    log.info(
        "rate limiting: enabled=%s auth=%d/%ds global=%d/%ds",
        _rl_cfg.enabled, _rl_cfg.auth_max, _rl_cfg.auth_window,
        _rl_cfg.global_max, _rl_cfg.global_window,
    )
    # Eagerly allocate the proxy-task tracking set so `_track_proxy_task`
    # never writes to a frozen/started app dict (aiohttp emits a
    # DeprecationWarning for mutations after startup).
    app[PROXY_TASKS_KEY] = set()
    # Router identity DB: run Alembic migrations and (if applicable)
    # the env-var bootstrap before anything else, so a fresh deploy
    # has the users table ready before the first dashboard request
    # lands. The handler itself is sync; wrap in a tiny coroutine to
    # satisfy aiohttp's on_startup signature. Added in Phase 1 PR B
    # of the operator-login plan (prancy-napping-pie).
    app.on_startup.append(_init_router_identity_on_startup)
    app.on_startup.append(reconcile_on_startup)
    app.on_startup.append(_start_reaper_task)
    app.on_startup.append(_start_alias_reaper_task)
    # `on_shutdown` runs before `on_cleanup` and before aiohttp's
    # `shutdown_timeout` countdown begins — cancelling in-flight
    # proxy tasks here lets the runner exit promptly. See
    # `_drain_proxy_tasks` for the why.
    app.on_shutdown.append(_drain_proxy_tasks)
    app.on_cleanup.append(shutdown)

    # Index + project lifecycle.
    #
    # Phase 3.5a (ADR-0009): the bare `/agent-mcp/` URL no longer renders
    # an HTML index page; it 302-redirects to the React overview at
    # `/agent-mcp/__dashboard/` (multi-tenant) or to the configured
    # single project's dashboard (single-tenant). The legacy
    # `index_handler` and its private HTML-render helpers were deleted
    # in Phase 6 — see the comment above ``index_handler``.
    app.router.add_get("/agent-mcp/", index_handler)
    app.router.add_get(
        "/agent-mcp",
        lambda r: web.HTTPMovedPermanently(location="/agent-mcp/"),
    )
    # ADR 0014: every operator-facing endpoint that used to live at
    # ``/agent-mcp/__*`` is now REST-shaped under
    # ``/agent-mcp/api/router/...``. Wired below alongside the per-
    # project ``/api/<name>/...`` proxy so the explicit admin paths
    # win in aiohttp's source-order dispatcher.

    # Dashboard surface — PR-B Shape-3 rename. Three top-level prefixes:
    #
    #   /agent-mcp/assets/<rest>     →  Next.js static bundle (was
    #                                   /agent-mcp/__dashboard/_next/<rest>).
    #                                   Top-level prefix decouples assets
    #                                   from any project segment so one
    #                                   on-disk tree serves all projects.
    #   /agent-mcp/app/              →  React overview (cross-project
    #                                   cards). Was /agent-mcp/__dashboard/.
    #   /agent-mcp/app/<name>/<rest> →  per-project dashboard page HTML.
    #                                   Was /agent-mcp/__dashboard/<name>/<rest>.
    #
    # The Next.js asset URLs inside the served HTML are rewritten at
    # serve time from the sentinel __AGENT_MCP_ASSET_PREFIX__ to the
    # value of ASSET_PREFIX (now /agent-mcp/assets by default) — see
    # ``_serve_dashboard_file`` + the Phase 4 substitution module.
    app.router.add_get(
        "/agent-mcp/assets/{rest:.*}", dashboard_assets_handler
    )
    app.router.add_get("/agent-mcp/app/", overview_dashboard_handler)
    app.router.add_get(
        "/agent-mcp/app",
        lambda r: web.HTTPMovedPermanently(location="/agent-mcp/app/"),
    )
    app.router.add_get(
        "/agent-mcp/app/{name}",
        lambda req: web.HTTPMovedPermanently(
            location=f"/agent-mcp/app/{req.match_info['name']}/"
        ),
    )
    app.router.add_get("/agent-mcp/app/{name}/", dashboard_handler)
    app.router.add_get(
        "/agent-mcp/app/{name}/{rest:.*}", dashboard_handler
    )

    # v5.0.0: the 30-day grace-window 308 redirects from the legacy
    # ``__dashboard/`` paths (and the legacy ``__dashboard/_next/``
    # asset paths) have been deleted. Those URLs now 404.

    # Backend operations.
    #
    # PR-D moved the MCP Streamable HTTP transport (dvaerum/Agent-MCP
    # 3.0.0; MCP spec rev 2025-03-26) from /agent-mcp/<name>/mcp to
    # the Shape-3 /agent-mcp/mcp/<name> top-level prefix. The four
    # reserved names (api, app, assets, mcp) — declared in
    # ``_validate_name`` — ensure no project shadows the top-level
    # segments now that `mcp` is one of them.
    app.router.add_route(
        "*", "/agent-mcp/mcp/{name}", backend_mcp_handler
    )
    # v5.0.0: the 30-day grace-window 308 redirect from the legacy
    # /agent-mcp/<name>/mcp path has been deleted. Old clients that
    # never updated their .mcp.json now 404.
    # Admin REST surface (ADR 0014) — every handler under
    # ``/agent-mcp/api/router/...``. Mounted BEFORE the catch-all
    # ``/api/<name>/<rest>`` proxy so the explicit admin paths win in
    # aiohttp's source-order dispatcher. The Accept-header gate
    # (PR-A) is applied per route inside ``register_admin_routes``.
    from . import admin_api
    admin_api.register_admin_routes(app)
    # Phase 3 Wave 1b (prancy-napping-pie): operator-facing CRUD for
    # users, groups, and project memberships. Mounted alongside the
    # existing admin routes so the same operator-session gate applies.
    from . import admin_users_api
    admin_users_api.register_admin_users_routes(app)
    # Phase 3 Wave 3 (prancy-napping-pie): sysadmin-only SSO config
    # introspection endpoint. Read-only — the SSO config itself
    # travels via env vars, so dashboard mutations are out of scope
    # (the home-manager module owns the canonical config).
    from . import admin_sso_api
    admin_sso_api.register_admin_sso_routes(app)

    # Phase 1 PR C: login + setup-wizard routes. Registered AFTER the
    # /api routes so a project literally named "login" can't shadow
    # them — though "login" isn't in ``_RESERVED_NAMES`` because the
    # ``/agent-mcp/<name>/...`` shape was retired in PR-D; routes that
    # name "login" today live at the top-level ``/agent-mcp/login``,
    # not under a project segment, so the collision can't happen.
    register_login_routes(app)
    register_setup_routes(app)
    # Phase 3 Wave 3 (prancy-napping-pie): /agent-mcp/sso/{login,callback}
    # for the OIDC authorization-code flow. The proxy-header trust
    # mode (the other SSO front-end) is implemented inside the
    # operator-session middleware rather than as a separate route —
    # the trusted header is consulted on every request alongside the
    # session cookie.
    from . import sso as _sso_module
    _sso_module.register_sso_routes(app)

    # PR-B Shape-3 REST surface. Strict Accept-header gate (PR-A) still
    # applies — see ``backend_api_handler``.
    app.router.add_route(
        "*", "/agent-mcp/api/{name}/{rest:.*}", backend_api_handler
    )
    # v5.0.0: the 30-day grace-window 308 redirect from the legacy
    # /agent-mcp/__api/<name>/<rest> path to the renamed surface has
    # been deleted. Old REST clients now 404.
    # Phase 6 removed the transitional 410-Gone handlers for the
    # `/agent-mcp/__sse/<name>` and `/agent-mcp/__messages/<name>/...`
    # URLs (the SSE+messages transport from dvaerum/Agent-MCP <3.0.0).
    # Those URLs now hit aiohttp's default 404, which is the intent.

    # __bridge routes removed: dashboard now uses upstream REST
    # endpoints directly (dvaerum/Agent-MCP#12 + #22).

    return app


def main() -> None:
    # AGENT_MCP_ROUTER_HOST lets the VM module bind 0.0.0.0 so qemu's
    # user-mode hostfwd packets (which arrive on the guest's primary
    # IP, not loopback) can be served. Production deploys keep the
    # 127.0.0.1 default and front the router with nginx/Tailscale.
    # Resolve through the SHARED helper so the string bound here is the
    # same one the fail-closed guard classified (R6-F1).
    host = _resolve_bind_host()
    # `shutdown_timeout=3.0` caps the window aiohttp waits for
    # in-flight handlers to drain after `on_shutdown` fires. The
    # `_drain_proxy_tasks` hook cancels every tracked proxy task
    # before that window begins, so this is a defense-in-depth
    # ceiling rather than the primary mechanism. With the prior 60 s
    # default + systemd's `TimeoutStopSec`, the router routinely
    # earned a SIGKILL after 90 s with the dashboard's MCP channel
    # open; the 3 s value keeps us well inside the 15 s
    # `TimeoutStopSec` set in the home-manager module.
    web.run_app(
        make_app(),
        host=host,
        port=ROUTER_PORT,
        shutdown_timeout=3.0,
    )


if __name__ == "__main__":
    main()
