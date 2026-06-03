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

URL convention: every router-internal operation segment
starts with `__`, every other path segment under /agent-mcp/ is a
project name. Because project names are matched against a slug
regex that does not allow underscores, the two namespaces cannot
collide.

  GET  /agent-mcp/                            HTML index page
  GET  /agent-mcp/__projects                  JSON name list (dashboard picker)
  POST /agent-mcp/__create                    create project
  POST /agent-mcp/__create-agent              create a worker agent on a project
  POST /agent-mcp/__stop                      stop a project (refuses if busy)
  POST /agent-mcp/__unregister                drop project
  GET  /agent-mcp/__client-config/<n>.mcp.json[?agent=<id>]  download .mcp.json
  GET  /agent-mcp/__client-installer/<n>.sh[?agent=<id>]     download merge script

  *    /agent-mcp/<name>/mcp                  proxy → backend /mcp
                                              (Streamable HTTP transport,
                                              MCP spec rev 2025-03-26)
  *    /agent-mcp/__api/<name>/{rest}         proxy → backend /api/{rest}
  GET  /agent-mcp/__dashboard/<name>/{rest}   static Next.js export

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
                                 the router itself via __create /
                                 __unregister.
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
import json
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode

from aiohttp import ClientSession, ClientTimeout, UnixConnector, web

from . import project_registry  # sibling module — see ./project_registry.py
from . import asset_prefix as _asset_prefix  # Phase 4: runtime sentinel sub


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
SOCK_DIR = Path(os.environ["AGENT_MCP_SOCK_DIR"])
DASHBOARD_DIR = os.environ["AGENT_MCP_DASHBOARD_DIR"]
ROUTER_PORT = int(os.environ.get("AGENT_MCP_ROUTER_PORT", "1337"))
IDLE_SEC = int(os.environ.get("AGENT_MCP_IDLE_SEC", str(4 * 60 * 60)))
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
def _read_package_version() -> str:
    """Best-effort fork version, read once at module import.

    Prefers ``importlib.metadata.version`` (correct when the package
    is installed), falls back to reading ``pyproject.toml`` from the
    repo root (correct in editable / dev installs), final fallback to
    ``agent_mcp.__version__`` (stale, but never raises).
    """
    try:  # installed-package path
        from importlib.metadata import version as _pkg_version
        return _pkg_version("agent-mcp")
    except Exception:  # noqa: BLE001 — fall through to next path
        pass
    try:  # editable / dev path
        import tomllib  # py311+, available on the supported Python
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())
        v = data.get("project", {}).get("version")
        if v:
            return str(v)
    except Exception:  # noqa: BLE001 — fall through to last-resort
        pass
    try:
        from agent_mcp import __version__ as _legacy_version
        return str(_legacy_version)
    except Exception:  # noqa: BLE001
        return "0.0.0"


_PACKAGE_VERSION = _read_package_version()


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

# Activity timestamps per (name, role). role is always "backend"
# today (the dashboard is static, served by the router itself), but
# the dict shape is kept tuple-keyed so a future sidecar role
# drops in cleanly.
last_active: dict[tuple[str, str], float] = {}

# Per-project in-flight connection counter. Incremented when a
# proxied request enters _proxy_to_backend, decremented when it
# leaves (including on error/cancel). __stop refuses to act while
# the counter is non-zero so an SSE session isn't yanked mid-stream.
active_conns: dict[str, int] = defaultdict(int)

# Per-(name, role) lock serialising _ensure. The dashboard fires
# several parallel API calls on first load; without this each one
# raced systemctl independently — fastest wins, the rest see the
# unit in a transient state and issue a `restart`, causing a
# stop/start storm and a ~10 s window where requests 504.
ensure_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _ensure_lock(name: str, role: str) -> asyncio.Lock:
    key = (name, role)
    lock = ensure_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        ensure_locks[key] = lock
    return lock


@contextlib.asynccontextmanager
async def _track_connection(name: str):
    active_conns[name] += 1
    try:
        yield
    finally:
        active_conns[name] -= 1
        if active_conns[name] <= 0:
            active_conns.pop(name, None)


# Per-project agent-token cache. Keyed by project name, value is
# (expires_at, {token → agent_id}). The MCP messages handler hits
# this on every POST so a 3-second TTL is plenty.
_token_cache_ttl_sec = 3.0
_agent_token_cache: dict[str, tuple[float, dict[str, str]]] = {}


async def _agent_token_map(name: str) -> dict[str, str]:
    """{token: agent_id} for project `name`, freshly cached.

    Includes the Admin token under agent_id "Admin". Reads the
    backend's own /api/tokens via the project UDS — same code path
    the bridge handler uses internally. Returns {} on backend error
    rather than raising; callers are expected to treat that as
    "no auth available, refuse" via the empty mapping.
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
    admin = body.get("admin_token")
    if admin:
        mapping[admin] = "Admin"
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


# Reserved project names. After PR-B's URL rename these four segments
# are top-level paths on the router; a project literally named after
# one of them would become structurally unreachable behind the
# more-specific route registration (audit §2.6). Rejected at create /
# rename time so the registry never holds a name that would collide.
_RESERVED_NAMES = frozenset({"api", "app", "assets", "mcp"})


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


def _sock_path(name: str, role: str) -> Path:
    d = SOCK_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{role}.sock"


def _unit_name(name: str, role: str) -> str:
    if role == "backend":
        return f"agent-mcp@{name}.service"
    raise ValueError(f"unsupported role: {role!r}")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True
    )


def _is_active(unit: str) -> bool:
    return _systemctl("is-active", unit).returncode == 0


async def _ensure(name: str, role: str) -> Path:
    """Make sure the backend for (name, role) is running, return its sock.

    "Running" requires both is-active and the socket file existing;
    the backend's systemd unit can stay "active" while the socket
    has gone stale (e.g. after a backend crash mid-write). In that
    case we restart, not start.

    Serialised per (name, role) so a burst of parallel requests
    (e.g. the dashboard's first-paint fan-out of /status /agents
    /tasks /graph-data) only triggers one systemctl invocation.
    """
    if name not in _projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    unit = _unit_name(name, role)
    sock = _sock_path(name, role)

    async with _ensure_lock(name, role):
        needs_start = not _is_active(unit) or not sock.exists()
        if needs_start:
            action = "restart" if _is_active(unit) else "start"
            r = _systemctl(action, unit)
            if r.returncode != 0:
                raise web.HTTPInternalServerError(
                    reason=f"systemctl {action} {unit} failed: {r.stderr.strip()}"
                )
            for _ in range(200):  # ≤20 s for the socket file to appear
                if sock.exists() and sock.is_socket():
                    break
                await asyncio.sleep(0.1)
            else:
                raise web.HTTPGatewayTimeout(
                    reason=f"{unit} did not create {sock} within 20 s"
                )
        last_active[(name, role)] = time.time()
    return sock


# ── Backend proxy ────────────────────────────────────────────────────


def _resolve_project_or_alias(name: str) -> tuple[str, dict | None]:
    """Return (real_name, alias_entry) for the URL segment `name`.

    `alias_entry` is None if `name` is itself a real project, or the
    matching ``{"name", "expires_at"}`` alias entry if `name` is a
    grace-period alias of some other project. Raises ``HTTPNotFound``
    if neither resolution succeeds.

    Used by the backend MCP + REST proxy handlers to support the
    Phase 1b alias-with-grace-period decision (#4 in the plan):
    requests to an alias URL are transparently re-pointed at the
    backend for the real project, with a sentinel header injected so
    the backend can later surface the deprecation warning to clients
    (Phase 1c — `serverInfo.instructions`).
    """
    row = _REGISTRY.get(name)
    if row is not None:
        return name, None
    real_name = _REGISTRY.resolve_alias(name)
    if real_name is None:
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    # Re-read the real project's row to find the alias entry's
    # expires_at — `resolve_alias` only returns the name, so we look
    # it up here to populate the X-Agent-MCP-Alias header.
    real_row = _REGISTRY.get(real_name)
    alias_entry: dict | None = None
    if real_row is not None:
        for entry in real_row.get("aliases", []):
            if entry.get("name") == name:
                alias_entry = entry
                break
    return real_name, alias_entry


async def _proxy_to_backend(
    req: web.Request, name: str, backend_path: str,
    *, alias_info: tuple[str, str] | None = None,
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
    """
    sock = await _ensure(name, "backend")
    url = f"http://localhost{backend_path}"
    # Forward Authorization upstream — the fork's AuthHeaderMiddleware
    # (dvaerum/Agent-MCP#19) reads `Authorization: Bearer <token>` into
    # a ContextVar and injects into arguments.token when the JSON-RPC
    # body doesn't include one. Without forwarding the header, the
    # upstream fallback never triggers.
    headers = {
        k: v for k, v in req.headers.items()
        if k.lower() not in ("host", "content-length")
    }
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
    async with _track_connection(name):
        async with ClientSession(connector=connector, timeout=timeout) as sess:
            async with sess.request(
                req.method,
                url,
                headers=headers,
                data=req_body,
                params=req.rel_url.query,
            ) as up:
                # Pure pass-through: read the full upstream body, hand
                # it back as a complete `web.Response`. aiohttp then
                # serialises the outbound response with a correct
                # Content-Length and no chunked-transfer involvement,
                # so strict HTTP clients (curl, Claude Code's MCP
                # client) see a cleanly-terminated transfer.
                #
                # Why pure pass-through and not streaming: the legacy
                # SSE transport's manual pump (decode chunks, re-encode
                # on the way out) was a workaround from before we
                # owned the fork — it had to splice `data: /messages/`
                # bytes mid-stream. Streamable HTTP (PR #61) made the
                # /mcp URL stable so no rewriting is needed; without
                # rewriting, streaming bought nothing but a fragile
                # chunked-transfer interaction with aiohttp.
                #
                # Trade-off: tools that emit mid-response SSE progress
                # events are buffered until the tool completes (the
                # client sees nothing in the meantime). Agent-MCP's
                # tools don't do this today; if a future tool needs
                # incremental streaming, switch this caller to a
                # framing-preserving variant. See
                # tests/test_mcp_streaming.py for the regression guard.
                #
                # Disconnect detection still works: the surrounding
                # `_track_connection` context manager bumps/decrements
                # the active-connection counter for the lifecycle
                # reaper, and a client that drops mid-`await up.read()`
                # raises ConnectionResetError out of this scope.
                body = await up.read()
                last_active[(name, "backend")] = time.time()
                out_headers = {
                    k: v for k, v in up.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-length")
                }
                return web.Response(
                    body=body, status=up.status, headers=out_headers,
                )


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

    Auth pre-check at the router edge: when `Authorization: Bearer
    <token>` is present, validate against the project's agent set
    and 401 here rather than letting the backend reject downstream.
    Missing header → 401 too (the backend's own
    AuthHeaderMiddleware also gates /mcp; we just shift the bad-
    token reject one hop closer to the client for a faster failure).
    """
    name = req.match_info["name"]
    redirect = _maybe_single_tenant_redirect(req, name)
    if redirect is not None:
        return redirect
    bearer = _extract_bearer(req)
    if bearer is None:
        raise _unauthorized()
    # Resolve alias → real project. The token map is fetched against
    # the *real* project because alias resolution is transparent;
    # tokens are not per-alias.
    real_name, alias_entry = _resolve_project_or_alias(name)
    tokens = await _agent_token_map(real_name)
    if bearer not in tokens:
        raise _unauthorized()
    alias_info: tuple[str, str] | None = None
    if alias_entry is not None:
        # Stash on the request too so downstream observers (Phase 1c
        # telemetry, future audit log) can pick it up.
        req["resolved_via_alias"] = name
        req["resolved_project"] = real_name
        alias_info = (name, alias_entry.get("expires_at", ""))
    return await _proxy_to_backend(
        req, real_name, "/mcp", alias_info=alias_info,
    )


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


# ── Admin-as-Admin MCP call (create-agent helper only) ───────────────
#
# `_mcp_call_admin` is the last remaining piece of router-side MCP
# protocol manipulation. It opens a short-lived SSE session as Admin
# and issues a single tools/call, exclusively for the `__create-agent`
# form handler (which needs to seed a task via `assign_task` and then
# `create_agent` with that task_id). Upstream's `POST /api/create-agent`
# REST endpoint requires the admin token and a pre-existing task_ids
# list — and `assign_task` has no REST counterpart — so retiring this
# helper would need either an upstream `POST /api/tasks` endpoint or
# a `create-agent-with-seed-task` REST endpoint. Both are
# nice-to-haves; not in the critical path for 7f.
# FOLLOW-UP: upstream a `POST /api/tasks` route on the fork, then
# replace this with two `aiohttp` REST calls.


async def _mcp_call_admin(
    name: str, tool: str, arguments: dict, *, timeout: float = 20
) -> dict:
    """Open an SSE session as Admin and issue a single tools/call.

    Returns the raw `result` dict from the JSON-RPC response. Raises
    on transport/initialise failure so the caller can map to a 5xx.

    Only `create_agent_handler` calls this today; see the section
    header comment for the follow-up to retire it.
    """
    sock = await _ensure(name, "backend")
    timeout_obj = ClientTimeout(total=None, sock_read=None)
    connector = UnixConnector(path=str(sock))
    async with ClientSession(connector=connector, timeout=timeout_obj) as sess:
        # 1) fetch admin token from the backend's own REST.
        async with sess.get("http://localhost/api/tokens") as r:
            if r.status != 200:
                raise RuntimeError(f"GET /api/tokens → {r.status}")
            admin_token = (await r.json()).get("admin_token")
        if not admin_token:
            raise RuntimeError("backend did not return admin_token")

        # 2) open SSE, harvest endpoint URL + session_id.
        async with sess.get(
            "http://localhost/sse",
            headers={"Accept": "text/event-stream"},
        ) as sse:
            if sse.status != 200:
                raise RuntimeError(f"GET /sse → {sse.status}")
            messages_path: str | None = None
            inbox: dict[int, dict] = {}
            endpoint_ready = asyncio.Event()
            response_ready = asyncio.Event()

            async def reader() -> None:
                event = None
                data_lines: list[str] = []
                while True:
                    raw = await sse.content.readline()
                    if not raw:
                        return
                    s = raw.rstrip(b"\r\n").decode()
                    if s == "":
                        if event == "endpoint" and data_lines:
                            nonlocal_assign(data_lines[0])
                        elif event == "message" and data_lines:
                            try:
                                j = json.loads(data_lines[0])
                                rid = j.get("id")
                                if rid is not None:
                                    inbox[rid] = j
                                    if rid == 2:
                                        response_ready.set()
                            except json.JSONDecodeError:
                                pass
                        event = None
                        data_lines = []
                        continue
                    if s.startswith(":"):
                        continue
                    if s.startswith("event:"):
                        event = s[6:].strip()
                    elif s.startswith("data:"):
                        data_lines.append(s[5:].lstrip())

            def nonlocal_assign(d: str) -> None:
                nonlocal messages_path
                messages_path = d
                endpoint_ready.set()

            reader_task = asyncio.create_task(reader())
            try:
                await asyncio.wait_for(endpoint_ready.wait(), timeout=timeout)
                msg_url = f"http://localhost{messages_path}"

                # 3) initialise (id=1) — fire-and-forget, then tools/call (id=2).
                init = {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "router-bridge", "version": "0.1"},
                    },
                }
                async with sess.post(msg_url, json=init) as r:
                    if r.status not in (200, 202):
                        raise RuntimeError(f"initialize → {r.status}")
                # Brief wait so the server registers us before
                # tools/call lands.
                await asyncio.sleep(0.2)
                async with sess.post(
                    msg_url,
                    json={
                        "jsonrpc": "2.0", "method": "notifications/initialized",
                        "params": {},
                    },
                ) as r:
                    pass
                args_with_token = {"token": admin_token, **arguments}
                async with sess.post(
                    msg_url,
                    json={
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": tool, "arguments": args_with_token},
                    },
                ) as r:
                    if r.status not in (200, 202):
                        raise RuntimeError(f"tools/call {tool} → {r.status}")
                await asyncio.wait_for(response_ready.wait(), timeout=timeout)
                result = inbox[2].get("result", {})
            finally:
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass
    return result


# ── Dashboard static handler ─────────────────────────────────────────


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
    """Resolve `rest` against the dashboard root, refusing escapes."""
    candidate = (_DASHBOARD_ROOT / rest).resolve()
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


async def dashboard_handler(req: web.Request) -> web.StreamResponse:
    """Serve the Next.js page HTML at /agent-mcp/__dashboard/<name>/.

    The HTML's embedded `<script src=…>` URLs come pre-prefixed with
    the build-time sentinel ``__AGENT_MCP_ASSET_PREFIX__``; Phase 4's
    `_serve_dashboard_file` substitutes the configured runtime prefix
    (default ``/agent-mcp/__dashboard``) before the bytes go on the
    wire. Assets themselves are served by
    `dashboard_assets_handler` below.
    """
    name = req.match_info.get("name", "")
    if name:
        redirect = _maybe_single_tenant_redirect(req, name)
        if redirect is not None:
            return redirect
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


# ── Idle reaper + startup reconciliation ─────────────────────────────


async def reaper(app: web.Application) -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for key in list(last_active.keys()):
            if now - last_active[key] > IDLE_SEC:
                _systemctl("stop", _unit_name(*key))
                last_active.pop(key, None)


# ── Alias reaper ────────────────────────────────────────────────────


# Cadence between alias-reaper ticks. The grace period for aliases is
# measured in days (default 30), so a 60 s tick gives near-instant
# cleanup of past-due aliases without paying for a tighter loop. Held
# at module scope so tests can monkeypatch to a smaller value.
_ALIAS_REAPER_INTERVAL_SEC = 60


async def _alias_reaper_tick(registry: project_registry.ProjectRegistry) -> None:
    """Single pass over the registry, removing any alias whose
    `expires_at` is in the past. Emits one INFO log line per removal.

    Extracted from the loop so tests can call it directly with a
    deterministic registry instance — the loop wrapper just wakes up,
    calls this, and goes back to sleep.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for row in registry.list():
        name = row["name"]
        for entry in list(row.get("aliases", []) or []):
            alias_name = entry.get("name")
            expires_at = entry.get("expires_at", "")
            if not alias_name or not expires_at:
                continue
            try:
                exp = project_registry._parse_iso(expires_at)
            except (TypeError, ValueError):
                # Malformed entry — leave it; the operator can clean
                # up by hand and we don't want to silently drop data
                # we can't reason about.
                continue
            if exp <= now:
                registry.expire_alias(name, alias_name)
                log.info(
                    "Alias %r for project %r expired and was removed.",
                    alias_name, name,
                )


async def alias_reaper(app: web.Application) -> None:
    """Long-running background task: every
    ``_ALIAS_REAPER_INTERVAL_SEC`` seconds, sweep the registry for
    aliases whose grace period has lapsed. Idempotent w.r.t.
    concurrent rename/add_alias — `expire_alias` is a write-locked
    operation.
    """
    while True:
        await asyncio.sleep(_ALIAS_REAPER_INTERVAL_SEC)
        try:
            await _alias_reaper_tick(_REGISTRY)
        except Exception:
            log.exception("alias reaper tick failed; will retry next pass")


async def reconcile_on_startup(app: web.Application) -> None:
    """Adopt already-running backend units after a router restart."""
    r = _systemctl(
        "list-units",
        "--type=service",
        "--state=active",
        "--no-legend",
        "--plain",
        "agent-mcp@*",
    )
    now = time.time()
    prefix = "agent-mcp@"
    suffix = ".service"
    for line in r.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if unit.startswith(prefix) and unit.endswith(suffix):
            name = unit[len(prefix) : -len(suffix)]
            last_active[(name, "backend")] = now


async def shutdown(app: web.Application) -> None:
    """No-op: backends are systemd-supervised and outlive the router."""
    return


# ── Create / unregister handlers ─────────────────────────────────────


async def projects_handler(req: web.Request) -> web.Response:
    """GET /agent-mcp/__projects — list of registered project names.

    Consumed by the dashboard's project picker so it can offer
    cross-project navigation (each pick becomes a navigation to
    /agent-mcp/__dashboard/<name>/). Sorted for stable UI.
    """
    return web.json_response(
        {"projects": sorted(_projects_dict().keys())},
        headers={"Cache-Control": "no-store"},
    )


# ── Overview endpoint (Phase 3.5a) ──────────────────────────────────
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


def _build_overview_envelope() -> dict:
    """Assemble the overview JSON envelope from registry + systemd +
    per-project SQLite. Cheap-ish; cached in `_overview_cache`."""
    now = time.time()
    projects_out: list[dict] = []
    for row in _REGISTRY.list():
        name = row["name"]
        workspace = row["workspace"]
        unit = _unit_name(name, "backend")
        running = _is_active(unit)
        ts = last_active.get((name, "backend"))
        counts = _project_counts(workspace)
        projects_out.append(
            {
                "name": name,
                "workspace": workspace,
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


async def alias_usage_handler(req: web.Request) -> web.Response:
    """GET /agent-mcp/__alias-usage?alias=<name>

    Backs the dashboard's alias-chip expansion panel (Phase 3.5c).
    Resolves the alias to its real project, then queries the
    project's SQLite `mcp_sessions.alias_used` column (added in
    Phase 1c migration 0005) for the distinct agent_ids that have
    used the alias. The dashboard shows this list so the operator
    can see "who's still on the old name" before deciding to expire
    the alias early.

    Returns 404 if the alias isn't currently active on any project.
    Returns ``{alias, project, expires_at, agents}`` on success.
    """
    alias = (req.rel_url.query.get("alias") or "").strip()
    if not alias:
        raise web.HTTPBadRequest(reason="missing 'alias' query parameter")

    real_name = _REGISTRY.resolve_alias(alias)
    if real_name is None:
        raise web.HTTPNotFound(
            reason=f"alias {alias!r} is not active on any project"
        )

    row = _REGISTRY.get(real_name)
    if row is None:
        # Race: resolve_alias hit a row that was unregistered between
        # the two reads. Treat as 404.
        raise web.HTTPNotFound(reason=f"alias {alias!r} no longer resolves")

    # Find the alias's expires_at on the project's record.
    expires_at = ""
    for entry in row.get("aliases", []) or []:
        if entry.get("name") == alias:
            expires_at = entry.get("expires_at", "")
            break

    # Pull distinct agent_ids from the project's SQLite (best-effort —
    # missing DB or missing column → empty list, never raises).
    agents: list[str] = []
    db = _project_db_path(row["workspace"])
    if db.is_file():
        try:
            import sqlite3

            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
            try:
                cur = con.cursor()
                rows = cur.execute(
                    "SELECT DISTINCT agent_id FROM mcp_sessions "
                    "WHERE alias_used = ? AND agent_id IS NOT NULL",
                    (alias,),
                ).fetchall()
                agents = [r[0] for r in rows if r[0]]
            finally:
                con.close()
        except Exception:
            # Table missing in a half-migrated DB, or unexpected DB
            # error — return [] rather than 500 the panel.
            log.exception("alias-usage: query failed for %s", db)

    return web.json_response(
        {
            "alias": alias,
            "project": real_name,
            "expires_at": expires_at,
            "agents": agents,
        },
        headers={"Cache-Control": "no-store"},
    )


async def remove_alias_handler(req: web.Request) -> web.Response:
    """POST /agent-mcp/__remove-alias

    Form fields: ``name`` (the real project), ``alias`` (the alias
    to drop). Removes the alias immediately, skipping the grace
    reaper. Surface on the dashboard via the alias-chip expansion's
    "Remove alias now" button (Phase 3.5c).

    Disabled in single-tenant mode (Phase 3) — there's no rename
    surface in N=1 mode, so there's no alias surface either.
    """
    if SINGLE_TENANT_NAME is not None:
        return _single_tenant_disabled_response()
    form = await req.post()
    name = (form.get("name") or "").strip()
    alias = (form.get("alias") or "").strip()
    if not name or not alias:
        raise web.HTTPBadRequest(reason="missing 'name' or 'alias'")

    row = _REGISTRY.get(name)
    if row is None:
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")

    _REGISTRY.expire_alias(name, alias)

    updated = _REGISTRY.get(name)
    remaining = list((updated or {}).get("aliases", []) or [])
    return web.json_response(
        {
            "removed": alias,
            "project": name,
            "remaining_aliases": remaining,
        },
        headers={"Cache-Control": "no-store"},
    )


async def overview_handler(req: web.Request) -> web.Response:
    """GET /agent-mcp/__overview — JSON envelope for the dashboard
    overview cards (R2 + S2 + multi-line per Phase 3.5).

    Cached for ``_OVERVIEW_CACHE_TTL_SEC`` to avoid hammering
    systemctl + SQLite on the dashboard's first-paint fan-out. The
    cache is process-local; one router process = one cache."""
    global _overview_cache
    now = time.time()
    if _overview_cache is not None and _overview_cache[0] > now:
        envelope = _overview_cache[1]
    else:
        envelope = _build_overview_envelope()
        _overview_cache = (now + _OVERVIEW_CACHE_TTL_SEC, envelope)
    return web.json_response(envelope, headers={"Cache-Control": "no-store"})


async def create_handler(req: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/__create — register a new project.

    Workspace location is fixed at the nix-managed
    DEFAULT_WORKSPACE_PARENT — the caller picks the name only.
    Any body-supplied 'workspace' field is silently ignored.

    In single-tenant mode (Phase 3) this endpoint returns 410 with
    ``endpoint_disabled_in_single_tenant_mode`` so the dashboard
    overview can show the operator-facing 'unavailable' explanation.
    """
    if SINGLE_TENANT_NAME is not None:
        return _single_tenant_disabled_response()
    form = await req.post()
    name = (form.get("name") or "").strip()

    # Slug + length validation first — we don't want to even mkdir
    # workspace for an invalid name. `_validate_name`'s third arm
    # (already-registered check) is now redundant with
    # `_REGISTRY.register()`'s own conflict detection, but keep it
    # so the error surfaces as a 400 with a friendly message rather
    # than as the registry's ValueError → 500.
    err = _validate_name(name, _projects_dict())
    if err is not None:
        raise web.HTTPBadRequest(reason=err)

    workspace = (DEFAULT_WORKSPACE_PARENT / name).expanduser().resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise web.HTTPBadRequest(
            reason=f"could not create workspace {workspace}: {e.strerror}"
        )

    # The race Candidate B targets lives here: previously
    # `_read_projects()` and `_write_projects()` were separate calls
    # with no lock between them, so two parallel POSTs to __create
    # could both read an N-entry registry, both append, and the
    # second write would clobber the first. `_REGISTRY.register()`
    # does the read-modify-write under a single LOCK_EX so the worst
    # case is now two serialised registrations rather than one lost
    # entry. Idempotency is harmless here because we already
    # rejected duplicate names above.
    try:
        _REGISTRY.register(name, str(workspace))
    except ValueError as e:
        # Lost the race against another __create with the same name —
        # rare, but a real possibility after we released the snapshot
        # we used for _validate_name.
        raise web.HTTPBadRequest(reason=str(e))

    raise web.HTTPSeeOther(
        location="/agent-mcp/?" + urlencode({"created": name})
    )


async def stop_handler(req: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/__stop — stop a project's backend.

    Refuses with 409 if the project still has active connections —
    pulling the rug from under a live SSE session would surface as
    a misleading "backend crash" to the connected client. The user
    can either disconnect those clients first, or wait for the
    idle reaper.
    """
    form = await req.post()
    name = (form.get("name") or "").strip()
    if not name:
        raise web.HTTPBadRequest(reason="missing 'name'")
    if name not in _projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")

    conns = active_conns.get(name, 0)
    if conns > 0:
        raise web.HTTPConflict(
            reason=f"{name!r} has {conns} active connection(s); refusing to stop"
        )

    unit = _unit_name(name, "backend")
    if _is_active(unit):
        r = _systemctl("stop", unit)
        if r.returncode != 0:
            raise web.HTTPInternalServerError(
                reason=f"systemctl stop {unit} failed: {r.stderr.strip()}"
            )

    wants_json = "application/json" in req.headers.get("Accept", "")
    if wants_json:
        return web.json_response(
            {"stopped": name}, headers={"Cache-Control": "no-store"}
        )
    raise web.HTTPSeeOther(
        location="/agent-mcp/?" + urlencode({"stopped": name})
    )


async def rename_handler(req: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/__rename — rename a project with a grace-period alias.

    Form-encoded body: ``old_name``, ``new_name``, optional
    ``grace_days`` (default 30). Plan decision #4 (locked) makes this
    a *full* rename: the workspace dir is moved on disk, the systemd
    unit name follows the new project name, per-agent token files
    are moved, and the old name is parked as an alias of the new
    name for ``grace_days`` days.

    The order of operations matters: side-effects (stop unit + move
    dir + move tokens) happen BEFORE the registry write so a partial
    failure leaves the on-disk state in a recoverable place. If the
    workspace path doesn't end in ``/<old_name>`` (operator-customised
    layout), we skip the directory rename — the caller can rename
    by hand.

    Refusals:

      * 400 if either name fails the slug regex.
      * 409 if ``new_name`` is already a project or active alias.
      * 409 if ``old_name`` has any in-flight MCP/REST connections,
        with the active-connection count surfaced in the body so the
        operator knows what's blocking them.
      * 404 if ``old_name`` isn't registered.

    In single-tenant mode (Phase 3) this endpoint returns 410 — the
    sole project's name is configured statically by the operator's
    home-manager profile, and there's no value in supporting rename
    of an N=1 set.
    """
    if SINGLE_TENANT_NAME is not None:
        return _single_tenant_disabled_response()
    form = await req.post()
    old_name = (form.get("old_name") or "").strip()
    new_name = (form.get("new_name") or "").strip()
    grace_raw = (form.get("grace_days") or "").strip()
    try:
        grace_days = int(grace_raw) if grace_raw else 30
    except ValueError:
        raise web.HTTPBadRequest(reason=f"grace_days must be an integer; got {grace_raw!r}")

    if not _SLUG_RE.match(old_name):
        raise web.HTTPBadRequest(
            reason=f"old_name must match {_SLUG_RE.pattern}"
        )
    if not _SLUG_RE.match(new_name):
        raise web.HTTPBadRequest(
            reason=f"new_name must match {_SLUG_RE.pattern}"
        )
    if old_name == new_name:
        raise web.HTTPBadRequest(reason="old_name and new_name are identical")

    old_row = _REGISTRY.get(old_name)
    if old_row is None:
        raise web.HTTPNotFound(reason=f"unknown project: {old_name!r}")

    # Conflict check before doing any side-effects. The registry's
    # own `rename()` would catch this too, but doing it here keeps
    # the error a 409 instead of a 500-out-of-ValueError.
    if _REGISTRY.get(new_name) is not None:
        raise web.HTTPConflict(
            reason=f"project {new_name!r} is already registered",
            text=json.dumps(
                {
                    "error": "name_taken",
                    "reason": f"project {new_name!r} is already registered",
                }
            ),
            content_type="application/json",
        )
    # Active alias collision check — `_REGISTRY.rename()` does this
    # too, but again, we want a 409 not a 500.
    if _REGISTRY.resolve_alias(new_name) is not None:
        raise web.HTTPConflict(
            reason=f"name {new_name!r} is an active alias",
            text=json.dumps(
                {
                    "error": "alias_collision",
                    "reason": f"name {new_name!r} is an active alias",
                }
            ),
            content_type="application/json",
        )

    # In-flight session refusal. `active_conns` is the router's
    # in-process per-(name) counter, incremented by `_track_connection`
    # at proxy entry and decremented on exit. Note this does NOT
    # cover sessions opened against the backend directly (bypassing
    # the router), but for the deploy-shape the deployed router is
    # the only ingress, so this counter is exhaustive.
    conns = active_conns.get(old_name, 0)
    if conns > 0:
        raise web.HTTPConflict(
            reason=(
                f"{old_name!r} has {conns} active connection(s); "
                f"refusing to rename"
            ),
            text=json.dumps(
                {
                    "error": "active_sessions",
                    "active_connections": conns,
                    "agents": [],  # per-agent attribution arrives in 1c
                    "reason": (
                        f"{old_name!r} has {conns} active connection(s); "
                        f"disconnect them and retry"
                    ),
                }
            ),
            content_type="application/json",
        )

    # Side-effects, in order.
    #
    # 1) Stop the systemd unit for the old name. Idempotent: a
    #    project that was never started just no-ops.
    _systemctl("stop", _unit_name(old_name, "backend"))

    # 2) Move the workspace dir if the path looks like .../<old_name>.
    #    Custom layouts (operators who pointed a project at a path
    #    that doesn't end in <old_name>) are left alone — they can
    #    rename by hand.
    workspace = Path(old_row.get("workspace", ""))
    new_workspace: Path | None = None
    if workspace.name == old_name and workspace.exists():
        new_workspace = workspace.with_name(new_name)
        try:
            os.rename(workspace, new_workspace)
        except OSError as e:
            raise web.HTTPInternalServerError(
                reason=f"could not rename workspace dir: {e.strerror}"
            )

    # 3) Move the per-agent token files. The on-disk naming follows
    #    `<project>--<agent>.token`; we glob for the old prefix and
    #    rename in place. Token dir lives at
    #    ~/.config/agent-mcp/tokens; missing dir is fine (no agents).
    token_dir = (
        Path(os.environ.get("AGENT_MCP_TOKENS_DIR", ""))
        or (Path.home() / ".config" / "agent-mcp" / "tokens")
    )
    if token_dir.is_dir():
        for tok in token_dir.glob(f"{old_name}--*.token"):
            suffix = tok.name[len(old_name) + len("--"):]
            try:
                tok.rename(token_dir / f"{new_name}--{suffix}")
            except OSError as e:
                log.warning(
                    "rename: token rename %s → %s--%s failed: %s",
                    tok, new_name, suffix, e.strerror,
                )

    # 4) Registry write last — if anything above raised, the on-disk
    #    state still has the old registry entry pointing at a missing
    #    workspace, which is a clear "rollback by hand" situation
    #    rather than the silently-broken alternative.
    try:
        _REGISTRY.rename(old_name, new_name, grace_days=grace_days)
    except (ValueError, KeyError) as e:
        # Best-effort rollback: put the workspace dir back. We don't
        # try to undo the unit stop (systemd will start on next
        # request) or token renames (those are idempotent on retry).
        if new_workspace is not None and new_workspace.exists():
            try:
                os.rename(new_workspace, workspace)
            except OSError:
                pass
        raise web.HTTPInternalServerError(
            reason=f"registry rename failed: {e}"
        )

    # Final response — the operator gets the alias's expiry so they
    # know when to expect the alias to disappear.
    new_row = _REGISTRY.get(new_name)
    alias_expires_at = ""
    for entry in (new_row or {}).get("aliases", []) or []:
        if entry.get("name") == old_name:
            alias_expires_at = entry.get("expires_at", "")
            break

    return web.json_response(
        {
            "renamed": True,
            "old": old_name,
            "new": new_name,
            "alias_expires_at": alias_expires_at,
        },
        headers={"Cache-Control": "no-store"},
    )


_DELETE_WORKSPACE_TRUTHY = {"true", "1", "yes", "on"}


def _form_truthy(form, key: str) -> bool:
    raw = (form.get(key) or "").strip().lower()
    return raw in _DELETE_WORKSPACE_TRUTHY


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
    except OSError:
        return False
    try:
        workspace_resolved.relative_to(parent_resolved)
    except ValueError:
        return False
    return True


async def unregister_handler(req: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/__unregister — drop a project.

    Stops the systemd unit. By default does NOT touch the workspace
    directory (the user's SQLite state lives there) nor any .mcp.json
    on any host (the router didn't create those).

    Phase 3.5b: the dashboard's two-tier safe-default remove modal
    (D4) can request a hard ``rm -rf`` of the workspace files by
    sending ``delete_workspace=true`` in the form body. The router
    only honours this when the workspace path resolves inside
    ``DEFAULT_WORKSPACE_PARENT`` (defence in depth — see
    ``_is_within_default_workspace`` for why).

    Phase 3.5b: refuse with 409 + a structured body listing active
    connection count when the project has any in-flight router-
    tracked sessions. The dashboard surfaces this list and asks the
    operator to disconnect agents before retrying.

    In single-tenant mode (Phase 3) this endpoint returns 410 — the
    project is fixed at module-config time, can't be unregistered
    over HTTP.
    """
    if SINGLE_TENANT_NAME is not None:
        return _single_tenant_disabled_response()
    form = await req.post()
    name = (form.get("name") or "").strip()
    if not name:
        raise web.HTTPBadRequest(reason="missing 'name'")

    # Take a snapshot for the existence check so we can return a 404
    # for a missing name rather than silently no-op'ing.
    # `_REGISTRY.unregister()` itself is idempotent-on-missing, which
    # is the right semantics for the registry but the wrong UX here.
    projects = _projects_dict()
    if name not in projects:
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")

    # Active-session refusal — mirrors the same guard on __rename
    # (Phase 1b). The dashboard's remove modal pre-fetches the active
    # agent list and asks the operator to terminate them first, but
    # we double-check here in case a session opened between modal-open
    # and confirm-click.
    conns = active_conns.get(name, 0)
    if conns > 0:
        raise web.HTTPConflict(
            reason=(
                f"{name!r} has {conns} active connection(s); "
                f"refusing to unregister"
            ),
            text=json.dumps(
                {
                    "error": "active_sessions",
                    "active_connections": conns,
                    "agents": [],
                    "reason": (
                        f"{name!r} has {conns} active connection(s); "
                        f"disconnect them and retry"
                    ),
                }
            ),
            content_type="application/json",
        )

    workspace_path = Path(projects[name])
    want_delete = _form_truthy(form, "delete_workspace")

    workspace_deleted = False
    workspace_delete_skipped_reason: str | None = None
    if want_delete:
        if not _is_within_default_workspace(workspace_path):
            workspace_delete_skipped_reason = (
                f"workspace {workspace_path} resolves outside the "
                f"default workspace parent; refusing recursive delete"
            )
            log.warning(
                "unregister: %s — %s", name, workspace_delete_skipped_reason
            )
        elif workspace_path.exists():
            import shutil
            try:
                shutil.rmtree(workspace_path)
                workspace_deleted = True
            except OSError as e:
                workspace_delete_skipped_reason = (
                    f"rmtree({workspace_path}) failed: {e.strerror}"
                )
                log.warning(
                    "unregister: %s — %s",
                    name, workspace_delete_skipped_reason,
                )
        else:
            workspace_delete_skipped_reason = (
                f"workspace {workspace_path} did not exist on disk"
            )
            # Treat as success — the operator's desired end-state is
            # "no workspace dir", and they have it.
            workspace_deleted = True

    _REGISTRY.unregister(name)
    _systemctl("stop", _unit_name(name, "backend"))

    wants_json = "application/json" in req.headers.get("Accept", "")
    if wants_json or want_delete:
        # The delete-workspace flow always wants the structured body
        # so the dashboard can surface partial-success (project gone
        # but workspace skipped). Pure unregister callers without an
        # explicit Accept header still get the legacy 303 redirect.
        body = {
            "unregistered": name,
            "workspace_deleted": workspace_deleted,
        }
        if workspace_delete_skipped_reason is not None:
            body["workspace_delete_skipped_reason"] = (
                workspace_delete_skipped_reason
            )
        return web.json_response(body, headers={"Cache-Control": "no-store"})

    raise web.HTTPSeeOther(location="/agent-mcp/")


# ── Wiring helpers (client-config / installer) ───────────────────────


def _mcp_url_for(name: str) -> str:
    """Public URL of the project's Streamable HTTP /mcp endpoint."""
    return f"{EXTERNAL_URL}/agent-mcp/{name}/mcp"


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
    raise web.HTTPNotFound(reason=f"unknown agent {agent_id!r} on {name!r}")


async def client_config_handler(req: web.Request) -> web.Response:
    """GET /agent-mcp/__client-config/<name>.mcp.json[?agent=<id>]"""
    name = req.match_info["name"]
    if name not in _projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    agent_id = req.rel_url.query.get("agent")
    token, _aid = await _resolve_agent_token(name, agent_id)
    body = json.dumps(_mcp_json_for(name, token=token), indent=2) + "\n"
    return web.Response(
        body=body,
        content_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename=".mcp.json"',
            "Cache-Control": "no-store",
        },
    )


async def client_installer_handler(req: web.Request) -> web.Response:
    """GET /agent-mcp/__client-installer/<name>.sh[?agent=<id>]"""
    name = req.match_info["name"]
    if name not in _projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    agent_id = req.rel_url.query.get("agent")
    token, _aid = await _resolve_agent_token(name, agent_id)
    return web.Response(
        text=_installer_script_for(name, token=token),
        content_type="text/x-shellscript",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def create_agent_handler(req: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/__create-agent

    Form-encoded body: name=<project>&agent_id=<new-agent-id>.
    Creates a worker agent via the existing MCP bridge (with
    send_prompt:false so we don't spawn a tmux session) and
    redirects back to the index page with the wiring panel
    pre-selected on the new agent.
    """
    form = await req.post()
    name = (form.get("name") or "").strip()
    agent_id = (form.get("agent_id") or "").strip()
    if not name:
        raise web.HTTPBadRequest(reason="missing 'name'")
    if not agent_id:
        raise web.HTTPBadRequest(reason="missing 'agent_id'")
    if name not in _projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    if not _SLUG_RE.match(agent_id):
        raise web.HTTPBadRequest(
            reason=f"agent_id must match {_SLUG_RE.pattern}"
        )
    # Upstream's create_agent requires a non-empty task_ids list. Seed
    # an "agent: bootstrap" task and attach the new agent to it.
    try:
        seed = await _mcp_call_admin(
            name,
            "assign_task",
            {
                "task_title": f"agent {agent_id}: bootstrap",
                "task_description": (
                    "Auto-created so the agent has at least one task "
                    "to attach to. Close or repurpose freely."
                ),
                "auto_suggest_parent": False,
                "validate_agent_workload": False,
                "override_rag": True,
                "override_reason": "router create-agent helper",
            },
        )
    except (asyncio.TimeoutError, Exception) as e:
        raise web.HTTPBadGateway(reason=f"seed task failed: {e}")
    seed_text = "\n".join(
        p.get("text", "") for p in seed.get("content", []) or []
    )
    m = re.search(r"task_\d+", seed_text)
    if not m:
        raise web.HTTPBadGateway(
            reason="seed task created but task_id not parseable"
        )
    seed_task_id = m.group(0)

    try:
        result = await _mcp_call_admin(
            name,
            "create_agent",
            {
                "agent_id": agent_id,
                "task_ids": [seed_task_id],
                "send_prompt": False,
            },
        )
    except (asyncio.TimeoutError, Exception) as e:
        raise web.HTTPBadGateway(reason=f"create_agent failed: {e}")
    text = "\n".join(
        p.get("text", "") for p in result.get("content", []) or []
    )
    if result.get("isError") or text.lstrip().lower().startswith("error"):
        raise web.HTTPBadRequest(reason=text[:200] or "create_agent error")
    # Invalidate token cache so the freshly-created agent shows up.
    _agent_token_cache.pop(name, None)
    raise web.HTTPSeeOther(
        location=(
            f"/agent-mcp/?{urlencode({'wiring': name, 'agent': agent_id})}"
            f"#wiring-{quote(name)}"
        )
    )


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
    return {
        "service": "agent-mcp",
        "version": _PACKAGE_VERSION,
        "mode": "single-tenant" if SINGLE_TENANT_NAME is not None else "multi-tenant",
        "endpoints": {
            "api": "/agent-mcp/api",
            "app": "/agent-mcp/app",
            "assets": "/agent-mcp/assets",
            # PR-D will fold this into /agent-mcp/mcp/<name>; today the
            # MCP transport is at /agent-mcp/<name>/mcp so the parent
            # prefix is just /agent-mcp.
            "mcp": "/agent-mcp",
        },
        "projects_url": "/agent-mcp/__projects",
        "overview_url": "/agent-mcp/__overview",
        "single_tenant_project": SINGLE_TENANT_NAME,
    }


def _make_rename_redirect(old_prefix: str, new_prefix: str):
    """Return an aiohttp handler that 308-redirects ``old_prefix/<rest>``
    to ``new_prefix/<rest>`` while preserving the request's method,
    body, and query string.

    PR-B uses this for the 30-day grace period after the URL rename so
    operators with hard-coded paths (bookmarks, scripts, external
    services) don't break the day the rename lands. 308 (Permanent
    Redirect) is the right status: 301 dropped POST → GET historically
    on some clients; 307/308 preserve method explicitly per RFC 7231.

    The old prefix is matched as a literal prefix in the path; the
    suffix (everything after) gets concatenated onto the new prefix.
    Query strings ride through via ``req.path_qs`` (path + ``?…``).
    """
    async def handler(req: web.Request) -> web.Response:
        path_qs = req.path_qs  # includes ?query=… if present
        assert path_qs.startswith(old_prefix), (
            f"_make_rename_redirect mismatch: expected prefix "
            f"{old_prefix!r}, got path {path_qs!r}"
        )
        new_path_qs = new_prefix + path_qs[len(old_prefix):]
        raise web.HTTPPermanentRedirect(location=new_path_qs)
    return handler


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

    app = web.Application()
    app.on_startup.append(reconcile_on_startup)
    app.on_startup.append(_start_reaper_task)
    app.on_startup.append(_start_alias_reaper_task)
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
    app.router.add_get("/agent-mcp/__projects", projects_handler)
    app.router.add_get("/agent-mcp/__overview", overview_handler)
    app.router.add_post("/agent-mcp/__create", create_handler)
    app.router.add_post("/agent-mcp/__create-agent", create_agent_handler)
    app.router.add_post("/agent-mcp/__stop", stop_handler)
    app.router.add_post("/agent-mcp/__unregister", unregister_handler)
    app.router.add_post("/agent-mcp/__rename", rename_handler)
    # Alias management (Phase 3.5c).
    app.router.add_get("/agent-mcp/__alias-usage", alias_usage_handler)
    app.router.add_post("/agent-mcp/__remove-alias", remove_alias_handler)

    # Client wiring helpers.
    app.router.add_get(
        "/agent-mcp/__client-config/{name}.mcp.json", client_config_handler
    )
    app.router.add_get(
        "/agent-mcp/__client-installer/{name}.sh", client_installer_handler
    )

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

    # Old Phase-6 paths kept alive as 308 redirects for ~30 days so
    # external services and bookmarks survive the rename. 308 (not 302)
    # preserves the HTTP method and request body across the redirect —
    # important for POST /agent-mcp/__api/<name>/<rest> calls that
    # carry a JSON body. Query strings ride through via req.path_qs.
    #
    # The _next/ tail under __dashboard/ split off to /assets/ rather
    # than /app/ — the assets bundle is now top-level, decoupled from
    # the dashboard pages path. Register the more-specific _next/
    # redirect first so it wins over the generic /__dashboard/ redirect.
    app.router.add_get(
        "/agent-mcp/__dashboard/_next/{rest:.*}",
        _make_rename_redirect("/agent-mcp/__dashboard/_next", "/agent-mcp/assets/_next"),
    )
    app.router.add_get(
        "/agent-mcp/__dashboard/",
        _make_rename_redirect("/agent-mcp/__dashboard", "/agent-mcp/app"),
    )
    app.router.add_get(
        "/agent-mcp/__dashboard",
        _make_rename_redirect("/agent-mcp/__dashboard", "/agent-mcp/app"),
    )
    app.router.add_get(
        "/agent-mcp/__dashboard/{name}",
        _make_rename_redirect("/agent-mcp/__dashboard", "/agent-mcp/app"),
    )
    app.router.add_get(
        "/agent-mcp/__dashboard/{name}/",
        _make_rename_redirect("/agent-mcp/__dashboard", "/agent-mcp/app"),
    )
    app.router.add_get(
        "/agent-mcp/__dashboard/{name}/{rest:.*}",
        _make_rename_redirect("/agent-mcp/__dashboard", "/agent-mcp/app"),
    )

    # Backend operations.
    #
    # /agent-mcp/<name>/mcp is the Streamable HTTP transport
    # (dvaerum/Agent-MCP 3.0.0; MCP spec rev 2025-03-26). PR-D will move
    # this to /agent-mcp/mcp/<name>; PR-B keeps the per-project shape so
    # the dashboard + REST rename can land without the MCP client-config
    # rewrite churn that PR-D will carry. The four reserved names (api,
    # app, assets, mcp) ensure no project shadows a top-level segment;
    # see ``_validate_name``.
    app.router.add_route(
        "*", "/agent-mcp/{name}/mcp", backend_mcp_handler
    )
    # PR-B Shape-3 REST surface. Strict Accept-header gate (PR-A) still
    # applies — see ``backend_api_handler``.
    app.router.add_route(
        "*", "/agent-mcp/api/{name}/{rest:.*}", backend_api_handler
    )
    # Old REST path — 308-redirects to the renamed surface. 308 lets
    # the redirect carry the original method + body, including POST
    # JSON bodies for the writes the dashboard used to send to /__api.
    app.router.add_route(
        "*", "/agent-mcp/__api/{name}/{rest:.*}",
        _make_rename_redirect("/agent-mcp/__api", "/agent-mcp/api"),
    )
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
    host = os.environ.get("AGENT_MCP_ROUTER_HOST", "127.0.0.1")
    web.run_app(make_app(), host=host, port=ROUTER_PORT)


if __name__ == "__main__":
    main()
