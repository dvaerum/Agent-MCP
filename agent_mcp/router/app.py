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

Retired endpoints (return 410 Gone with a JSON migration body
pointing at /agent-mcp/<name>/mcp):

  *    /agent-mcp/__sse/<name>                old SSE handshake
  *    /agent-mcp/__messages/<name>/{rest}    old paired POST endpoint

dvaerum/Agent-MCP 3.0.0 dropped the SSE+messages pair in favour of
a single stateless Streamable HTTP /mcp endpoint. Sessions are no
longer issued or required; backend restarts are invisible to
clients beyond the in-flight request itself.

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
  AGENT_MCP_README_HTML          optional rendered README fragment
                                 shown in the index page's
                                 "How to use" details block.
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
README_HTML_PATH = os.environ.get("AGENT_MCP_README_HTML", "")
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


# ── Streamable HTTP migration ────────────────────────────────────
# dvaerum/Agent-MCP 3.0.0 dropped the SSE+messages pair in favour of
# a single `POST/GET/DELETE /mcp` endpoint. The router exposes it
# at `/agent-mcp/<name>/mcp` and 410s the old `/agent-mcp/__sse/`
# and `/agent-mcp/__messages/` shapes so any client/config still
# pointed at them gets a structured, parseable hint pointing at the
# new URL. See the backend's `_MIGRATION_BODY` in main_app.py.

_MIGRATION_BODY = json.dumps(
    {
        "error": "endpoint_removed",
        "migrated_to": "/mcp",
        "spec_revision": "2025-03-26",
        "hint": (
            "Use POST /agent-mcp/<name>/mcp with "
            "Authorization: Bearer <token>. "
            "Sessions are no longer required."
        ),
    },
).encode("utf-8")


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


async def legacy_sse_gone_handler(req: web.Request) -> web.Response:
    """/agent-mcp/__sse/<name> → 410 with migration body."""
    return web.Response(
        status=410,
        body=_MIGRATION_BODY,
        content_type="application/json",
    )


async def legacy_messages_gone_handler(req: web.Request) -> web.Response:
    """/agent-mcp/__messages/<name>/{rest} → 410 with migration body."""
    return web.Response(
        status=410,
        body=_MIGRATION_BODY,
        content_type="application/json",
    )


async def backend_api_handler(req: web.Request) -> web.StreamResponse:
    """/agent-mcp/__api/<name>/{rest} → backend /api/{rest}"""
    rest = req.match_info.get("rest", "")
    name = req.match_info["name"]
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


async def dashboard_handler(req: web.Request) -> web.StreamResponse:
    """Serve the Next.js page HTML at /agent-mcp/__dashboard/<name>/.

    The HTML's embedded `<script src=…>` URLs come pre-prefixed by
    Next.js's `assetPrefix` (set in the build-time patch to
    `/agent-mcp/__dashboard`), so they get served by
    dashboard_assets_handler below, NOT by this handler. This one
    only deals with the page itself (index.html or any nested
    page route from the static export).
    """
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
    ctype = _MIME.get(candidate.suffix.lower(), "application/octet-stream")
    # HTML may change between rebuilds (different chunk hashes embedded
    # in <script> tags). Force fresh fetches so a redeploy doesn't get
    # masked by the browser disk cache.
    return web.FileResponse(
        path=candidate,
        headers={"Content-Type": ctype, "Cache-Control": "no-store"},
    )


async def dashboard_assets_handler(req: web.Request) -> web.StreamResponse:
    """Serve Next.js static assets at /agent-mcp/__dashboard/_next/…

    The dashboard's `assetPrefix` patch makes Next emit asset URLs as
    `/agent-mcp/__dashboard/_next/static/…` — no project name segment
    (assetPrefix is fixed at build time, can't include a runtime
    project name). All projects share the same on-disk dist tree
    anyway, so a single route serving from DASHBOARD_DIR/_next/...
    is the right thing.
    """
    rest = req.match_info.get("rest", "")
    candidate = _safe_dashboard_path(f"_next/{rest}")
    if candidate is None or not candidate.is_file():
        raise web.HTTPNotFound()
    ctype = _MIME.get(candidate.suffix.lower(), "application/octet-stream")
    # Next.js content-hashes every chunk filename; the same URL is
    # guaranteed to map to the same bytes forever. Mark immutable so
    # the browser skips even conditional revalidation on reload.
    return web.FileResponse(
        path=candidate,
        headers={
            "Content-Type": ctype,
            "Cache-Control": "public, max-age=31536000, immutable",
        },
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


async def create_handler(req: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/__create — register a new project.

    Workspace location is fixed at the nix-managed
    DEFAULT_WORKSPACE_PARENT — the caller picks the name only.
    Any body-supplied 'workspace' field is silently ignored.
    """
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
    """
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


async def unregister_handler(req: web.Request) -> web.StreamResponse:
    """POST /agent-mcp/__unregister — drop a project.

    Stops the systemd unit; does NOT touch the workspace directory
    (the user's SQLite state lives there) nor any .mcp.json on any
    host (the router didn't create those).
    """
    form = await req.post()
    name = (form.get("name") or "").strip()
    if not name:
        raise web.HTTPBadRequest(reason="missing 'name'")

    # Take a snapshot for the existence check so we can return a 404
    # for a missing name rather than silently no-op'ing.
    # `_REGISTRY.unregister()` itself is idempotent-on-missing, which
    # is the right semantics for the registry but the wrong UX here.
    if name not in _projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    _REGISTRY.unregister(name)

    _systemctl("stop", _unit_name(name, "backend"))

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


# ── Index page ───────────────────────────────────────────────────────


def _list_view() -> list[dict]:
    rows: list[dict] = []
    now = time.time()
    for name, path in sorted(_projects_dict().items()):
        running = _is_active(_unit_name(name, "backend"))
        ts = last_active.get((name, "backend"))
        idle_for = (now - ts) if (running and ts is not None) else None
        rows.append(
            dict(name=name, path=path, running=running, idle_for=idle_for)
        )
    return rows


_INDEX_STYLE = """\
<style>
body { font-family: system-ui, sans-serif; max-width: 60em; margin: 2em auto; padding: 0 1em; }
h1 { margin-bottom: 0.2em; }
h2 { margin-top: 1.5em; }
table { border-collapse: collapse; width: 100%; margin-top: 1em; }
td, th { padding: 0.4em 0.8em; border-bottom: 1px solid #ddd; text-align: left; vertical-align: top; }
code { background: #f4f4f4; padding: 0 0.3em; border-radius: 3px; }
pre { background: #f4f4f4; padding: 0.7em 0.9em; border-radius: 4px; overflow-x: auto; }
pre code { background: none; padding: 0; }
.running { color: #2a8; }
.stopped { color: #999; }
details { margin: 1em 0; padding: 0.7em 0.9em; background: #f7f7f7; border-radius: 4px; }
summary { cursor: pointer; font-weight: 600; }
form { display: inline; margin: 0; }
button { font: inherit; cursor: pointer; }
fieldset { margin: 1em 0; padding: 0.8em 1.2em; border: 1px solid #ddd; border-radius: 4px; }
fieldset legend { padding: 0 0.4em; font-weight: 600; }
fieldset label { display: block; margin: 0.4em 0; }
fieldset input[type=text] { width: 100%; max-width: 32em; padding: 0.3em 0.4em; font: inherit; }
fieldset .hint { color: #888; font-size: 0.85em; margin-left: 0.5em; }
fieldset button { margin-top: 0.5em; padding: 0.3em 0.9em; }
.created { background: #ecfdf5; border: 1px solid #6ee7b7; padding: 0.8em 1.2em; border-radius: 4px; margin: 1em 0; }
.created h3 { margin-top: 0; }
.agent-picker, .agent-create { margin: 0.4em 0; }
.agent-picker select, .agent-create input { font: inherit; padding: 0.2em 0.4em; }
.agent-picker label, .agent-create label { font-weight: 600; }
.hint { color: #666; font-size: 0.92em; }
.warn { color: #b45309; font-size: 0.92em; }
</style>
"""


async def _wiring_help_panel(
    name: str, *, opened: bool, selected_agent: str = "Admin"
) -> str:
    """Render the wiring panel for project `name`, scoped to one agent.

    Lists every agent on the project as a dropdown; the four
    copy-paste blocks below are pre-filled with the selected
    agent's token so the user can copy-paste straight into
    `.mcp.json` / `claude mcp add`. A "Create new agent" mini-form
    lets the user spin up a fresh worker without leaving the page.
    """
    tokens = await _agent_token_map(name)
    # Stable order: Admin first, then alphabetical.
    agent_ids = ["Admin"] + sorted(a for a in tokens.values() if a != "Admin")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    agent_ids = [a for a in agent_ids if not (a in seen or seen.add(a))]
    missing_agent_warning = ""
    if selected_agent not in agent_ids:
        missing_agent_warning = (
            f'<p class=warn>No agent <code>{escape(selected_agent)}</code> '
            f"on this project — falling back to the default.</p>"
        )
        selected_agent = "Admin" if "Admin" in agent_ids else (agent_ids[0] if agent_ids else "Admin")
    token_for: dict[str, str] = {a: t for t, a in tokens.items()}
    sel_token = token_for.get(selected_agent, "")

    mcp_url = _mcp_url_for(name)
    auth_flag = (
        f' --header "Authorization: Bearer {sel_token}"' if sel_token else ""
    )
    cli_cmd = f"claude mcp add --transport http agent-mcp {mcp_url}{auth_flag}"
    json_snippet = json.dumps(
        _mcp_json_for(name, token=sel_token if sel_token else None), indent=2,
    )
    qs = urlencode({"agent": selected_agent})
    cfg_url = (
        f"{EXTERNAL_URL}/agent-mcp/__client-config/{name}.mcp.json?{qs}"
    )
    inst_url = (
        f"{EXTERNAL_URL}/agent-mcp/__client-installer/{name}.sh?{qs}"
    )
    installer = _installer_script_for(
        name, token=sel_token if sel_token else None
    )
    open_attr = " open" if opened else ""

    # Agent picker: a tiny GET form that reloads the same page with
    # ?wiring=<name>&agent=<id>.
    options = "".join(
        f'<option value="{escape(a)}"{" selected" if a == selected_agent else ""}>{escape(a)}</option>'
        for a in agent_ids
    )
    picker = (
        f'<form method=get action="/agent-mcp/" class="agent-picker">'
        f'<input type=hidden name=wiring value="{escape(name)}">'
        f'<label>Show wiring for: '
        f'<select name=agent onchange="this.form.submit()">{options}</select>'
        f'</label>'
        f'<noscript> <button type=submit>Switch</button></noscript>'
        f'</form>'
    )
    create_form = (
        f'<form method=post action="/agent-mcp/__create-agent" class="agent-create">'
        f'<input type=hidden name=name value="{escape(name)}">'
        f'<label>Create new agent: '
        f'<input type=text name=agent_id required '
        f'pattern="[a-z][a-z0-9-]*[a-z0-9]|[a-z]" placeholder="e.g. backend-dev">'
        f'</label> <button type=submit>Create</button>'
        f'</form>'
    )

    if not sel_token:
        identity_note = (
            "<p class=warn><strong>Heads up:</strong> the backend isn't "
            "running, so we couldn't fetch agent tokens. The blocks below "
            "show the URL only; they'll fill in once a backend request "
            "(e.g. opening the dashboard) starts the project.</p>"
        )
    elif selected_agent == "Admin":
        identity_note = (
            "<p class=hint><strong>Admin scope.</strong> Use this for "
            "yourself or for the orchestrator. For each developer machine, "
            "create a per-machine agent below so revocation is granular.</p>"
        )
    else:
        identity_note = (
            f'<p class=hint>Wiring shown is scoped to agent '
            f'<code>{escape(selected_agent)}</code>.</p>'
        )

    return f"""
<details class=wiring{open_attr}>
  <summary>Wiring help for <code>{escape(name)}</code></summary>
  {missing_agent_warning}
  {picker}
  {create_form}
  {identity_note}

  <h4>1. <code>claude mcp add</code> (writes to user-global ~/.claude.json)</h4>
<pre><code>{escape(cli_cmd)}</code></pre>

  <h4>2. JSON snippet to drop into a project's <code>.mcp.json</code></h4>
<pre><code>{escape(json_snippet)}</code></pre>

  <h4>3. One-liner — download <code>.mcp.json</code> to cwd</h4>
<pre><code>curl -fsSL '{escape(cfg_url)}' -o .mcp.json</code></pre>

  <h4>4. One-liner — merge into existing <code>.mcp.json</code> (or create)</h4>
<pre><code>curl -fsSL '{escape(inst_url)}' | bash</code></pre>
  <details>
    <summary>What does the installer script do? (audit before piping to bash)</summary>
<pre><code>{escape(installer)}</code></pre>
  </details>
</details>"""


def _create_form() -> str:
    return f"""
<fieldset>
  <legend>Create a new project</legend>
  <form method=post action="/agent-mcp/__create" style="display:block">
    <label>Name <input type=text name=name required pattern="[a-z][a-z0-9-]*[a-z0-9]|[a-z]" maxlength={_NAME_MAX}></label>
    <button type=submit>Create</button>
  </form>
</fieldset>"""


async def index_handler(req: web.Request) -> web.Response:
    rows = _list_view()
    readme_html = ""
    if README_HTML_PATH and Path(README_HTML_PATH).is_file():
        try:
            readme_html = Path(README_HTML_PATH).read_text()
        except OSError:
            readme_html = ""

    created = req.rel_url.query.get("created", "")
    projects = _projects_dict()
    # Which project's wiring panel (if any) should open by default,
    # and on which agent. Set when the user just created a project
    # (?created=…), just created an agent (?wiring=…&agent=…), or
    # clicked a wiring link (?wiring=…).
    wiring_focus_name = (
        req.rel_url.query.get("wiring") or created or ""
    )
    wiring_focus_agent = (
        req.rel_url.query.get("agent") or "Admin"
    )
    created_panel = ""
    if created and created in projects:
        wiring = await _wiring_help_panel(
            created, opened=True, selected_agent=wiring_focus_agent
        )
        created_panel = (
            f'<div class=created><h3>✓ Project <code>{escape(created)}</code> created</h3>'
            f'<p>Workspace: <code>{escape(projects[created])}</code></p>'
            f'{wiring}'
            "</div>"
        )

    stopped = req.rel_url.query.get("stopped", "")
    stopped_panel = ""
    if stopped and stopped in projects:
        stopped_panel = (
            f'<div class=created><h3>■ Project <code>{escape(stopped)}</code> stopped</h3>'
            "<p>The backend systemd unit was stopped cleanly. Any new "
            "request to its dashboard or SSE URL will spin it back up.</p>"
            "</div>"
        )

    parts: list[str] = [
        "<!doctype html><meta charset=utf-8><title>agent-mcp</title>",
        _INDEX_STYLE,
        "<h1>agent-mcp</h1>",
        "<details><summary>How to use</summary>",
        readme_html or "<p><em>README not bundled.</em></p>",
        "</details>",
        _create_form(),
        created_panel,
        stopped_panel,
        "<h2>projects</h2>",
    ]
    if not rows:
        parts.append(
            "<p><em>No projects registered yet.</em> Use the "
            "<strong>Create a new project</strong> form above.</p>"
        )
    else:
        parts.append(
            "<table><tr><th>name<th>path<th>state<th>actions</tr>"
        )
        for r in rows:
            if r["running"] and r["idle_for"] is not None:
                state = (
                    f'<span class=running>running '
                    f'({int(r["idle_for"])}s idle)</span>'
                )
            elif r["running"]:
                state = '<span class=running>running</span>'
            else:
                state = '<span class=stopped>stopped</span>'
            n = r["name"]
            dashboard_link = (
                f'<a href="/agent-mcp/__dashboard/{quote(n)}/">dashboard</a>'
            )
            help_link = (
                f'<a href="#wiring-{quote(n)}">wiring</a>'
            )
            stop_form = ""
            if r["running"]:
                conn_count = active_conns.get(n, 0)
                conn_note = (
                    f" Will be refused: {conn_count} active connection(s)."
                    if conn_count else ""
                )
                stop_form = (
                    '<form method=post action="/agent-mcp/__stop"'
                    f' onsubmit="return confirm(\'Stop {escape(n)}?{escape(conn_note)}\')">'
                    f'<input type=hidden name=name value="{escape(n)}">'
                    '<button type=submit>stop</button></form>'
                )
            unreg_form = (
                '<form method=post action="/agent-mcp/__unregister"'
                f' onsubmit="return confirm(\'unregister {escape(n)}?\')">'
                f'<input type=hidden name=name value="{escape(n)}">'
                '<button type=submit>unregister</button></form>'
            )
            parts.append(
                f"<tr><td>{escape(n)}<td><code>{escape(r['path'])}</code>"
                f"<td>{state}<td>{dashboard_link} {help_link} "
                f"{stop_form} {unreg_form}</tr>"
            )
        parts.append("</table>")
        # Per-project wiring panels (collapsed, anchored). The
        # ?wiring=<name>[&agent=<id>] focus opens its panel and
        # selects the right agent in its dropdown.
        for r in rows:
            n = r["name"]
            opened = (n == wiring_focus_name and not created)
            agent = wiring_focus_agent if opened else "Admin"
            wiring = await _wiring_help_panel(
                n, opened=opened, selected_agent=agent
            )
            parts.append(f'<div id="wiring-{escape(n)}">')
            parts.append(wiring)
            parts.append("</div>")
    return web.Response(
        text="".join(parts),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


# ── Wire-up ──────────────────────────────────────────────────────────


async def _start_reaper_task(app: web.Application) -> None:
    asyncio.create_task(reaper(app))


async def _start_alias_reaper_task(app: web.Application) -> None:
    """Spawn the alias reaper coroutine on app startup. Mirrors the
    pattern used by `_start_reaper_task` for the idle-backend reaper."""
    asyncio.create_task(alias_reaper(app))


def make_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(reconcile_on_startup)
    app.on_startup.append(_start_reaper_task)
    app.on_startup.append(_start_alias_reaper_task)
    app.on_cleanup.append(shutdown)

    # Index + project lifecycle.
    app.router.add_get("/agent-mcp/", index_handler)
    app.router.add_get(
        "/agent-mcp",
        lambda r: web.HTTPMovedPermanently(location="/agent-mcp/"),
    )
    app.router.add_get("/agent-mcp/__projects", projects_handler)
    app.router.add_post("/agent-mcp/__create", create_handler)
    app.router.add_post("/agent-mcp/__create-agent", create_agent_handler)
    app.router.add_post("/agent-mcp/__stop", stop_handler)
    app.router.add_post("/agent-mcp/__unregister", unregister_handler)
    app.router.add_post("/agent-mcp/__rename", rename_handler)

    # Client wiring helpers.
    app.router.add_get(
        "/agent-mcp/__client-config/{name}.mcp.json", client_config_handler
    )
    app.router.add_get(
        "/agent-mcp/__client-installer/{name}.sh", client_installer_handler
    )

    # Dashboard. Two routes:
    #   - /agent-mcp/__dashboard/_next/...  →  shared static assets
    #     (one on-disk tree serves every project; Next.js's
    #     `assetPrefix` patch makes the HTML embed this exact path).
    #     MUST be registered before the project-aware route so the
    #     more-specific prefix wins.
    #   - /agent-mcp/__dashboard/<name>/... →  page HTML for the
    #     project (one on-disk index.html; the dashboard JS picks
    #     project from window.location.pathname for its API calls).
    app.router.add_get(
        "/agent-mcp/__dashboard/_next/{rest:.*}", dashboard_assets_handler
    )
    # Bare /agent-mcp/__dashboard/<name> (no trailing slash) is a common
    # typed/bookmarked URL; redirect to the canonical trailing-slash form
    # so the relative asset URLs in index.html resolve correctly.
    app.router.add_get(
        "/agent-mcp/__dashboard/{name}",
        lambda req: web.HTTPMovedPermanently(
            location=f"/agent-mcp/__dashboard/{req.match_info['name']}/"
        ),
    )
    app.router.add_get("/agent-mcp/__dashboard/{name}/", dashboard_handler)
    app.router.add_get(
        "/agent-mcp/__dashboard/{name}/{rest:.*}", dashboard_handler
    )

    # Backend operations.
    #
    # /agent-mcp/<name>/mcp is the new Streamable HTTP transport
    # (dvaerum/Agent-MCP 3.0.0; MCP spec rev 2025-03-26). The path
    # segment after the project name is `mcp` (3 lowercase letters),
    # which the project-name slug regex (`^[a-z](?:[a-z0-9-]*[a-z0-9])?$`)
    # could in principle also match — but the more-specific route
    # takes precedence in aiohttp's matching order, so a project
    # literally named "mcp" would lose access to its own dashboard
    # under that path. We accept that edge case; "mcp" is reserved.
    app.router.add_route(
        "*", "/agent-mcp/{name}/mcp", backend_mcp_handler
    )
    # Legacy 410s — kept so any client/config still pointed at the
    # old shape gets the structured migration hint instead of a
    # bare 404.
    app.router.add_route(
        "*", "/agent-mcp/__sse/{name}", legacy_sse_gone_handler
    )
    app.router.add_route(
        "*", "/agent-mcp/__messages/{name}/{rest:.*}",
        legacy_messages_gone_handler,
    )
    app.router.add_route(
        "*", "/agent-mcp/__api/{name}/{rest:.*}", backend_api_handler
    )

    # __bridge routes removed: dashboard now uses upstream REST
    # endpoints directly (dvaerum/Agent-MCP#12 + #22).

    return app


def main() -> None:
    web.run_app(make_app(), host="127.0.0.1", port=ROUTER_PORT)


if __name__ == "__main__":
    main()
