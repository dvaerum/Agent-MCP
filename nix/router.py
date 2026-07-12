#!/usr/bin/env python3
# Vendored from nixos-developer-system/users/dennis/agent-mcp/router.py.
# Single intentional fork from upstream: _systemctl honours
# AGENT_MCP_SYSTEMCTL_MODE so we can run as a system service in the
# NixOS VM (where there's no user systemd instance to talk to).
#
# NOT the deployed router (OBS1 vendored drift): every VM/module goes
# nix/vm.nix → nix/module.nix → nix/packages.nix `agentMcpRouterWrapper`
# → `python -m agent_mcp.cli router` → agent_mcp/router/app.py. This
# file is only exposed as the `agent-mcp-router` flake output and runs
# nowhere. The R8-F2 SSE-streaming proxy fix therefore lands in the
# package (agent_mcp/router/{app,project_orchestrator}.py), not here;
# fully de-vendoring this stale copy is tracked separately (OBS1).
"""
agent-mcp-router

Thin always-on HTTP proxy that fronts per-project agent-mcp
backends. URL convention: every router-internal operation segment
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

  *    /agent-mcp/__sse/<name>                proxy → backend GET /sse
  *    /agent-mcp/__messages/<name>/{rest}    proxy → backend /messages/{rest}
  *    /agent-mcp/__api/<name>/{rest}         proxy → backend /api/{rest}
  GET  /agent-mcp/__dashboard/<name>/{rest}   static Next.js export

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
                                 tree). `__AGENT_MCP_SSE_URL__`
                                 inside it gets substituted with
                                 the project's SSE URL at request
                                 time.
"""

import asyncio
import contextlib
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode

from aiohttp import ClientSession, ClientTimeout, UnixConnector, web

# ── Configuration ────────────────────────────────────────────────────

PROJECTS_FILE = Path(os.environ["AGENT_MCP_PROJECTS_FILE"])
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
INSTALLER_TEMPLATE_PATH = Path(os.environ["AGENT_MCP_INSTALLER_TEMPLATE"])
_INSTALLER_TEMPLATE = INSTALLER_TEMPLATE_PATH.read_text()

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

# Per-(name, session_id) injection queue. When the SSE pump captures
# a session_id from the `event: endpoint` rewrite, it registers a
# queue here; the messages handler pushes synthetic JSON-RPC SSE
# events onto it (e.g. responses for the router-synthesised
# `list_agents` tool) and the pump's drain task forwards them to the
# client interleaved with upstream chunks. Cleaned up when the SSE
# session ends.
_pending_sse_queues: dict[tuple[str, str], asyncio.Queue[bytes]] = {}


# Synthetic tools the router itself implements — never forwarded to
# the upstream backend. Listed in tools/list, handled inline by
# backend_messages_handler. Currently:
#   list_agents — read-only peer discovery. Returns the project's
#                 agents list (id, status, color, current_task)
#                 without needing any token. Workers can use it to
#                 find peers for send_agent_message; upstream's
#                 view_status is admin-only so workers had no
#                 first-contact path.
_SYNTHETIC_TOOLS: list[dict] = [
    {
        "name": "list_agents",
        "description": (
            "List the *active* agents on this project (agent_id, "
            "status, color, current_task). Terminated agents are "
            "filtered out. Read-only; safe to call from any "
            "session. Use this to discover peers for "
            "send_agent_message — `view_status` is admin-only "
            "upstream so workers have no other first-contact path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_tasks_for",
        "description": (
            "List the tasks currently assigned to a given agent on "
            "this project (task_id, title, status, priority, "
            "description preview). Use this to look up what a peer "
            "is working on before send_agent_message-ing them. "
            "Read-only; safe to call from any session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "The agent whose tasks you want to see, "
                        "e.g. 'ios-app-dev'. Get the available "
                        "values from list_agents."
                    ),
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_unassigned_tasks",
        "description": (
            "List tasks that have no assignee yet — the project's "
            "pool of available work. Use claim_task to take "
            "ownership of one. Read-only; safe to call from any "
            "session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_unassigned_task",
        "description": (
            "Create a new task that lives in the unassigned pool. "
            "Workers can use this to file work they discover but "
            "don't want to take on themselves; peers can then "
            "list_unassigned_tasks and claim_task to pick it up. "
            "Returns the new task_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short task title.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Detailed task description / acceptance "
                        "criteria. Be specific — peers see only "
                        "this when deciding whether to claim it."
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Task priority (default medium).",
                },
            },
            "required": ["title", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_peer_message",
        "description": (
            "Send a message to another agent on this project, "
            "*including peer-to-peer between workers*. Upstream's "
            "send_agent_message tool refuses worker→worker with "
            "'Communication denied: Communication not permitted "
            "between these agents'; this synthetic version relays "
            "the message through Admin so any pair can talk. The "
            "recipient sees the message prefixed with "
            "'[from <your agent_id>]' so they know who sent it. "
            "Use list_agents to find valid recipient_id values."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "string",
                    "description": (
                        "The agent_id of the peer to message, "
                        "e.g. 'ios-app-dev'."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "Body of the message.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                    "description": "Message priority (default normal).",
                },
            },
            "required": ["recipient_id", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task_for_self",
        "description": (
            "Create a new task and assign it to *yourself* (the "
            "agent identified by this session's bearer). Use "
            "this to track work you've decided to take on. "
            "Equivalent to create_unassigned_task → claim_task in "
            "one step. Returns the new task_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short task title.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Detailed task description / acceptance "
                        "criteria."
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Task priority (default medium).",
                },
            },
            "required": ["title", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task_for",
        "description": (
            "Create a new task and assign it to *another* agent "
            "(delegation). Use this when you want a specific peer "
            "to pick up the work. For self-assignment, use "
            "create_task_for_self instead. Upstream's assign_task "
            "drops the assignee silently when called with title + "
            "agent_token in one shot; this synthetic routes "
            "through Admin in two hops to make it actually stick. "
            "Returns the new task_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "Recipient agent_id from list_agents, "
                        "e.g. 'ios-app-dev'. Cannot be yourself — "
                        "use create_task_for_self for that."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Short task title.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Detailed task description / acceptance "
                        "criteria."
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Task priority (default medium).",
                },
            },
            "required": ["agent_id", "title", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_project_context",
        "description": (
            "Write a key/value entry into the shared project "
            "context (project memory). Upstream's "
            "update_project_context is admin-only — this "
            "synthetic version lets workers contribute shared "
            "knowledge without admin intervention. Re-using an "
            "existing key updates it. Safe to call from any "
            "authenticated session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context_key": {
                    "type": "string",
                    "description": (
                        "Key under which to store the value. "
                        "Workers should namespace their keys "
                        "(e.g. 'backend-dev/api-shape') to avoid "
                        "stepping on each other."
                    ),
                },
                "context_value": {
                    "description": (
                        "The value to store. Strings, numbers, "
                        "booleans, arrays, and objects are all "
                        "accepted."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Optional one-line description shown next "
                        "to the key in view_project_context."
                    ),
                },
            },
            "required": ["context_key", "context_value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_my_task_status",
        "description": (
            "Update the status of one of YOUR own tasks (or any "
            "task if you are Admin). Use this to mark a task "
            "in_progress when you start it, completed when you "
            "finish, or failed/cancelled if it's blocked. "
            "Upstream's update_task_status is admin-only; this "
            "synthetic version lets workers update their own task "
            "lifecycle. Tasks belonging to other agents can only "
            "be updated by Admin."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": (
                        "task_id to update (e.g. from list_tasks_for "
                        "or list_unassigned_tasks)."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed",
                             "cancelled", "failed"],
                    "description": "New status.",
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Optional notes attached to the status "
                        "transition (e.g. 'blocked on iOS task X', "
                        "'shipped in commit abc123')."
                    ),
                },
            },
            "required": ["task_id", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "claim_task",
        "description": (
            "Take ownership of an existing unassigned task. The "
            "task becomes assigned to you (the agent identified by "
            "this session's bearer token). Use "
            "list_unassigned_tasks to find available work first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": (
                        "The task_id to claim, e.g. "
                        "'task_1780157456706' "
                        "(from list_unassigned_tasks)."
                    ),
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
]
_SYNTHETIC_TOOL_NAMES = {t["name"] for t in _SYNTHETIC_TOOLS}

# Synthetic tools that only Admin sessions should see / be allowed to
# call. Worker sessions get them filtered out of tools/list and
# refused at call time. Today: cross-agent delegation. Workers should
# either claim work themselves (create_task_for_self,
# create_unassigned_task → claim_task) or post to the pool and let a
# peer pick it up.
_ADMIN_ONLY_SYNTHETIC: set[str] = {"create_task_for"}


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


def _read_projects() -> dict[str, str]:
    """Read the projects file. Missing/malformed → empty dict."""
    if not PROJECTS_FILE.is_file():
        return {}
    try:
        data = json.loads(PROJECTS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_projects(projects: dict[str, str]) -> None:
    """Atomically replace the projects file."""
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROJECTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(projects, indent=2) + "\n")
    tmp.replace(PROJECTS_FILE)


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


# Whether to call `systemctl --user` (default, matches the
# nixos-developer-system / home-manager deployment) or plain
# `systemctl` (system mode, used by the in-VM flake deployment).
_SYSTEMCTL_MODE = os.environ.get(
    "AGENT_MCP_SYSTEMCTL_MODE", "user"
).strip().lower()


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    base = ["systemctl"]
    if _SYSTEMCTL_MODE == "user":
        base.append("--user")
    return subprocess.run([*base, *args], capture_output=True, text=True)


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
    if name not in _read_projects():
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


def _redact_tokens_in_event(event_bytes: bytes, tokens: list[str]) -> bytes:
    """Replace every known project token with [redacted-token-…] inside
    the event body. Defensive: any tool can stash a token in its return
    value (agent-mcp itself does this — view_project_context echoes
    `config_admin_token` in plaintext to any caller that can read the
    project context). We don't trust the upstream to redact; we do it
    on the way back.
    """
    if not tokens:
        return event_bytes
    out = event_bytes
    for tok in tokens:
        if not tok:
            continue
        needle = tok.encode()
        if needle in out:
            mask = f"[redacted-token-{tok[:4]}]".encode()
            out = out.replace(needle, mask)
    return out


def _rewrite_tools_list_event(
    event_bytes: bytes, *, hide_admin_only: bool = False,
    project: str | None = None,
) -> bytes:
    """If `event_bytes` is an SSE event carrying a tools/list response,
    drop `token` from every tool's inputSchema and (optionally) hide
    admin-only tools entirely.

    Used when the client authenticated via Authorization header.
    `hide_admin_only=True` is set when the bearer maps to a worker
    agent: we walk each tool's *original* token property description
    and drop tools where it says "Admin authentication token" (the
    upstream convention) so the worker session doesn't see tools it
    can't use.
    """
    if b'"tools":' not in event_bytes:
        return event_bytes
    # Backend uses CRLF; normalise so reconstruction is deterministic.
    lines = event_bytes.replace(b"\r\n", b"\n").split(b"\n")
    data_lines: list[bytes] = []
    other_lines: list[bytes] = []
    for line in lines:
        if line.startswith(b"data:"):
            data_lines.append(line[len(b"data:"):].lstrip())
        else:
            other_lines.append(line)
    if not data_lines:
        return event_bytes
    try:
        payload = json.loads(b"\n".join(data_lines))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return event_bytes
    if not isinstance(payload, dict):
        return event_bytes
    result = payload.get("result")
    if not isinstance(result, dict):
        return event_bytes
    tools = result.get("tools")
    if not isinstance(tools, list):
        return event_bytes
    modified = False
    kept: list = []
    for tool in tools:
        if not isinstance(tool, dict):
            kept.append(tool)
            continue
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            kept.append(tool)
            continue
        props = schema.get("properties")
        is_admin_only = False
        tool_name = tool.get("name") or ""
        if isinstance(props, dict):
            tok_prop = props.get("token")
            if isinstance(tok_prop, dict):
                desc = (tok_prop.get("description") or "").lower()
                # Upstream agent-mcp uses "Admin authentication token"
                # on every admin-gated tool; non-admin tools just say
                # "Authentication token" (or similar).
                is_admin_only = "admin" in desc
        if hide_admin_only and is_admin_only:
            modified = True
            continue
        if isinstance(props, dict) and "token" in props:
            props.pop("token", None)
            modified = True
        req = schema.get("required")
        if isinstance(req, list) and "token" in req:
            schema["required"] = [r for r in req if r != "token"]
            modified = True
        kept.append(tool)
    # Always advertise the router-synthesised tools alongside the
    # upstream ones. Forces reconstruction even if nothing else
    # changed. When the bearer is a worker (hide_admin_only=True),
    # also drop synthetic tools marked admin-only.
    for syn in _SYNTHETIC_TOOLS:
        if hide_admin_only and syn["name"] in _ADMIN_ONLY_SYNTHETIC:
            continue
        kept.append(syn)
    result["tools"] = kept
    new_data = json.dumps(payload).encode()
    return b"\r\n".join(other_lines + [b"data: " + new_data])


async def _proxy_to_backend(
    req: web.Request, name: str, backend_path: str,
    *, body_override: bytes | None = None,
    rewrite_tools_list: bool = False,
    hide_admin_tools: bool = False,
    redact_tokens: list[str] | None = None,
) -> web.StreamResponse:
    """Proxy `req` to the backend for `name`, asking it for `backend_path`.

    `backend_path` is the path the agent-mcp backend itself expects
    (e.g. `/sse`, `/messages/?session_id=…`, `/api/agents`). The
    caller is responsible for translating the *router's* URL shape
    (`/agent-mcp/__sse/<name>`) into the *backend's* URL shape.

    `body_override` lets the messages handler replace the inbound
    JSON-RPC body after token injection. When set we use it as
    `data=` instead of streaming `req.content`.

    `rewrite_tools_list` makes the response pump buffer per SSE
    event and drop `token` from tools/list responses' inputSchema
    (only event with `"tools":` substring is reparsed; others pass
    through unchanged). Set when the SSE was authenticated via the
    Authorization header so the model sees a tokenless API.

    `hide_admin_tools` additionally drops admin-gated tools from
    the tools/list response — used when the bearer maps to a
    worker agent, so the model only sees tools it can actually
    call.

    `redact_tokens` lists strings to replace inside every SSE
    event's body before forwarding downstream. Used to scrub agent
    tokens out of tool responses (notably view_project_context,
    which echoes config_admin_token to anyone who can read).
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
    if body_override is not None:
        headers["Content-Length"] = str(len(body_override))
    timeout = ClientTimeout(total=None, sock_read=None)
    connector = UnixConnector(path=str(sock))

    # Agent-MCP's SSE handshake announces a messages endpoint as
    # `data: /messages/?session_id=…` — root-absolute, prefix-blind.
    # Rewrite in-flight so the client's POST lands at the path the
    # router actually routes to. Filed upstream as
    # https://github.com/rinadelph/Agent-MCP/issues/62.
    needle = b"data: /messages/"
    replacement = f"data: /agent-mcp/__messages/{name}/".encode()

    async with _track_connection(name):
        async with ClientSession(connector=connector, timeout=timeout) as sess:
            async with sess.request(
                req.method,
                url,
                headers=headers,
                data=body_override if body_override is not None else req.content,
                params=req.rel_url.query,
            ) as up:
                resp = web.StreamResponse(status=up.status, headers=up.headers)
                await resp.prepare(req)

                # Shared write lock so the upstream pump, the inject-
                # queue drainer, AND the disconnect watcher (which
                # writes SSE keepalive comments) all serialise their
                # writes — each whole event lands without interleaving.
                write_lock = asyncio.Lock()

                async def _write(b: bytes) -> None:
                    async with write_lock:
                        await resp.write(b)
                        last_active[(name, "backend")] = time.time()

                # Disconnect watcher. Polling req.transport.is_closing()
                # alone proved unreliable under HTTPS-through-tailscale:
                # the transport state didn't always flip after a
                # graceful client close, so the connection counter
                # stayed bumped and `__stop` refused indefinitely. The
                # SSE spec lets us send comment lines (": …") that
                # clients ignore — we use them as an active probe: a
                # 3-second keepalive that fails fast when the client
                # is gone, in addition to the cheap transport check.
                # Only meaningful on long-lived SSE responses; for the
                # short messages/api responses the pump finishes before
                # the first sleep so this never fires.
                async def _watch_disconnect() -> None:
                    # 1-second cycle: cheap transport check, then an
                    # SSE keepalive write that surfaces a broken peer
                    # as ConnectionResetError. Modern aiohttp drains
                    # inside write() so we don't need a separate
                    # drain() call (which is deprecated).
                    while True:
                        await asyncio.sleep(1.0)
                        t = req.transport
                        if t is None or t.is_closing():
                            return
                        try:
                            await _write(b": keepalive\r\n\r\n")
                        except (
                            ConnectionResetError,
                            asyncio.CancelledError,
                            __import__("aiohttp").ClientConnectionResetError,
                            Exception,
                        ):
                            return

                watcher = asyncio.create_task(_watch_disconnect())
                stream_task: asyncio.Task[None] | None = None

                async def _pump() -> None:
                    # We always run the per-event buffering + queue
                    # setup so the synthetic-tool path works even on
                    # legacy (unauthenticated) sessions; the
                    # rewrite/redaction are gated by
                    # rewrite_tools_list.
                    # Per-event buffering. SSE events are terminated
                    # by a blank line; agent-mcp uses CRLF, the spec
                    # also permits LF. Buffer until we see either, then
                    # let _rewrite_tools_list_event drop `token` from
                    # any tools/list responses before forwarding.
                    def _split_event(b: bytes) -> tuple[bytes, bytes, bytes] | None:
                        for sep in (b"\r\n\r\n", b"\n\n"):
                            idx = b.find(sep)
                            if idx >= 0:
                                return b[:idx], sep, b[idx + len(sep):]
                        return None

                    inject_queue: asyncio.Queue[bytes] | None = None
                    inject_key: tuple[str, str] | None = None

                    async def _drain_injections() -> None:
                        # Started after the SSE handshake announces a
                        # session_id; serves synthesised events
                        # (list_agents responses, future router tools).
                        assert inject_queue is not None
                        while True:
                            data = await inject_queue.get()
                            try:
                                await _write(data)
                            except Exception:
                                return

                    drain_task: asyncio.Task | None = None
                    session_id_re = re.compile(rb"session_id=([0-9a-f]+)")

                    buf = b""
                    async for chunk in up.content.iter_any():
                        if needle in chunk:
                            chunk = chunk.replace(needle, replacement)
                        buf += chunk
                        while True:
                            split = _split_event(buf)
                            if split is None:
                                break
                            event, sep, buf = split
                            # Capture the session_id from the SSE
                            # handshake (event: endpoint) so the
                            # messages handler can find this stream
                            # by session_id for synthetic-tool
                            # response injection.
                            if (
                                inject_queue is None
                                and b"event: endpoint" in event
                            ):
                                m = session_id_re.search(event)
                                if m:
                                    sid = m.group(1).decode()
                                    inject_queue = asyncio.Queue()
                                    inject_key = (name, sid)
                                    _pending_sse_queues[inject_key] = inject_queue
                                    drain_task = asyncio.create_task(
                                        _drain_injections()
                                    )
                            if rewrite_tools_list:
                                event = _rewrite_tools_list_event(
                                    event,
                                    hide_admin_only=hide_admin_tools,
                                    project=name,
                                )
                            if redact_tokens:
                                event = _redact_tokens_in_event(
                                    event, redact_tokens,
                                )
                            await _write(event + sep)
                    if buf:
                        try:
                            await _write(buf)
                        except Exception:
                            pass
                    # Cleanup the injection queue + drainer.
                    if inject_key is not None:
                        _pending_sse_queues.pop(inject_key, None)
                    if drain_task is not None:
                        drain_task.cancel()
                        try:
                            await drain_task
                        except (asyncio.CancelledError, Exception):
                            pass

                stream_task = asyncio.create_task(_pump())
                try:
                    done, _ = await asyncio.wait(
                        {watcher, stream_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # Surface unexpected stream errors; ignore the
                    # benign disconnect family.
                    for d in done:
                        try:
                            d.result()
                        except (
                            ConnectionResetError,
                            asyncio.CancelledError,
                            __import__("aiohttp").ClientConnectionResetError,
                        ):
                            pass
                finally:
                    for t in (watcher, stream_task):
                        if t and not t.done():
                            t.cancel()
                            with contextlib.suppress(Exception):
                                await t
                return resp


async def backend_sse_handler(req: web.Request) -> web.StreamResponse:
    """/agent-mcp/__sse/<name> → backend GET /sse

    When `Authorization: Bearer <token>` is present:
      - validate the token against the project's agent set,
      - rewrite tools/list responses to drop `token` from every
        tool's schema,
      - hide admin-gated tools from worker sessions (so the model
        doesn't waste turns trying things it can't do),
      - redact every known project token out of every event body
        on the way back (defensive: view_project_context echoes
        config_admin_token in plaintext upstream).

    Missing header → fall through to the legacy
    `arguments.token`-in-body flow with no rewriting.
    """
    name = req.match_info["name"]
    bearer = _extract_bearer(req)
    authenticated = bearer is not None
    redact: list[str] = []
    hide_admin = False
    if authenticated:
        tokens = await _agent_token_map(name)
        if bearer not in tokens:
            raise _unauthorized()
        # Everything we know about this project goes into the
        # redact list: any agent-token bytes that appear in a tool
        # response will be masked. Cheap and broad.
        redact = list(tokens.keys())
        hide_admin = tokens[bearer] != "Admin"
    return await _proxy_to_backend(
        req, name, "/sse",
        rewrite_tools_list=authenticated,
        hide_admin_tools=hide_admin,
        redact_tokens=redact,
    )


async def _fetch_agents(name: str) -> list[dict]:
    """Read the project's agents from the backend's own REST."""
    sock = await _ensure(name, "backend")
    connector = UnixConnector(path=str(sock))
    timeout = ClientTimeout(total=10)
    async with ClientSession(connector=connector, timeout=timeout) as sess:
        async with sess.get("http://localhost/api/agents") as r:
            if r.status != 200:
                raise RuntimeError(f"GET /api/agents → {r.status}")
            data = await r.json()
    return data if isinstance(data, list) else []


async def _fetch_tasks(name: str) -> list[dict]:
    """Read the project's tasks from the backend's own REST.
    Endpoint returns either a bare list or {tasks: [...]}.
    """
    sock = await _ensure(name, "backend")
    connector = UnixConnector(path=str(sock))
    timeout = ClientTimeout(total=10)
    async with ClientSession(connector=connector, timeout=timeout) as sess:
        async with sess.get("http://localhost/api/tasks") as r:
            if r.status != 200:
                raise RuntimeError(f"GET /api/tasks → {r.status}")
            data = await r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("tasks", []) or []
    return []


def _format_sse_event(payload: dict) -> bytes:
    """Wrap a JSON-RPC payload as a complete SSE 'message' event."""
    return (
        b"event: message\r\n"
        b"data: " + json.dumps(payload).encode() + b"\r\n\r\n"
    )


async def _handle_synthetic_tool_call(
    name: str, session_id: str, payload: dict, *, bearer: str | None = None,
) -> bool:
    """If `payload` is a tools/call for a router-synthesised tool,
    build the response and push it onto that session's inject queue.
    Returns True if handled (caller should 202 the POST without
    forwarding upstream), False otherwise.

    `bearer` is the calling session's agent token — needed by
    claim_task to know which agent is making the claim.
    """
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return False
    tool = params.get("name")
    if tool not in _SYNTHETIC_TOOL_NAMES:
        return False
    rpc_id = payload.get("id")
    queue = _pending_sse_queues.get((name, session_id))
    if queue is None:
        # No live SSE for this session_id — emit a JSON-RPC error
        # response so the client doesn't hang. Best-effort.
        return False
    try:
        if tool == "list_agents":
            agents_all = await _fetch_agents(name)
            agents = [
                a for a in agents_all
                if (a.get("status") or "").lower() != "terminated"
            ]
            text_lines = [f"active agents on project {name!r}:"]
            for a in agents:
                text_lines.append(
                    f"  {a.get('agent_id','?'):<30} "
                    f"status={a.get('status','?')} "
                    f"color={a.get('color','?')} "
                    f"current_task={a.get('current_task','?')}"
                )
            text = "\n".join(text_lines)
            result = {
                "content": [{"type": "text", "text": text}],
                # Echo the structured data alongside the human-readable
                # block so clients that want to parse can do so.
                "structuredContent": {"agents": agents},
                "isError": False,
            }
        elif tool == "list_unassigned_tasks":
            tasks_all = await _fetch_tasks(name)
            tasks = [
                t for t in tasks_all
                if not (t.get("assigned_to") or "").strip()
            ]
            if not tasks:
                text = f"No unassigned tasks on project {name!r}."
            else:
                lines = [f"unassigned tasks on project {name!r}:"]
                for t in tasks:
                    desc = (t.get("description") or "")
                    preview = (desc[:80] + "…") if len(desc) > 80 else desc
                    preview = preview.replace("\n", " ")
                    lines.append(
                        f"  {t.get('task_id','?')}  "
                        f"[{t.get('status','?')}/{t.get('priority','?')}]  "
                        f"{t.get('title','?')}"
                    )
                    if preview:
                        lines.append(f"      {preview}")
                text = "\n".join(lines)
            result = {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {"tasks": tasks},
                "isError": False,
            }
        elif tool == "create_unassigned_task":
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            title = (args.get("title") or "").strip()
            description = (args.get("description") or "").strip()
            priority = (args.get("priority") or "medium").strip().lower()
            if priority not in ("low", "medium", "high"):
                priority = "medium"
            if not title or not description:
                result = {
                    "content": [{"type": "text",
                                 "text": "Both 'title' and 'description' are required."}],
                    "isError": True,
                }
            else:
                # Use admin's assign_task with no agent_token = creates
                # an unassigned task.
                upstream = await _mcp_call_admin(
                    name, "assign_task", {
                        "task_title": title,
                        "task_description": description,
                        "priority": priority,
                        "auto_suggest_parent": False,
                        "validate_agent_workload": False,
                        "override_rag": True,
                        "override_reason": "router synthetic create_unassigned_task",
                    },
                )
                up_text = "\n".join(
                    p.get("text", "")
                    for p in upstream.get("content", []) or []
                )
                m = re.search(r"task_[A-Za-z0-9]+", up_text)
                task_id = m.group(0) if m else None
                if upstream.get("isError") or not task_id:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Upstream assign_task rejected: {up_text[:300]}"}],
                        "isError": True,
                    }
                else:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Created unassigned task {task_id}: {title}"}],
                        "structuredContent": {"task_id": task_id,
                                              "title": title,
                                              "priority": priority,
                                              "assigned_to": None},
                        "isError": False,
                    }
        elif tool == "claim_task":
            args = params.get("arguments") or {}
            task_id = (args.get("task_id") or "").strip() if isinstance(args, dict) else ""
            if not task_id:
                result = {
                    "content": [{"type": "text",
                                 "text": "Missing required argument 'task_id'."}],
                    "isError": True,
                }
            elif bearer is None:
                # claim_task is only meaningful for an authenticated
                # session — we need to know who's claiming.
                result = {
                    "content": [{"type": "text",
                                 "text": "claim_task requires an authenticated session "
                                         "(Authorization: Bearer <agent-token>)."}],
                    "isError": True,
                }
            else:
                # Sanity: task must exist and be unassigned.
                try:
                    tasks_all = await _fetch_tasks(name)
                except Exception as e:
                    tasks_all = []
                    fetch_err = str(e)
                else:
                    fetch_err = None
                row = next(
                    (t for t in tasks_all
                     if t.get("task_id") == task_id),
                    None,
                )
                tokens = await _agent_token_map(name)
                claimant_id = tokens.get(bearer, "<unknown>")
                if fetch_err is not None:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Could not fetch tasks: {fetch_err}"}],
                        "isError": True,
                    }
                elif row is None:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"No task with id {task_id!r}."}],
                        "isError": True,
                    }
                elif (row.get("assigned_to") or "").strip():
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Task {task_id!r} is already assigned to "
                                             f"{row.get('assigned_to')!r}; can't claim."}],
                        "isError": True,
                    }
                else:
                    # Upstream's assign_task with agent_token + task_ids
                    # re-assigns existing tasks.
                    upstream = await _mcp_call_admin(
                        name, "assign_task", {
                            "agent_token": bearer,
                            "task_ids": [task_id],
                            "auto_suggest_parent": False,
                            "validate_agent_workload": False,
                            "override_rag": True,
                            "override_reason": "router synthetic claim_task",
                        },
                    )
                    up_text = "\n".join(
                        p.get("text", "")
                        for p in upstream.get("content", []) or []
                    )
                    if upstream.get("isError"):
                        result = {
                            "content": [{"type": "text",
                                         "text": f"Upstream rejected claim: {up_text[:300]}"}],
                            "isError": True,
                        }
                    else:
                        # Upstream's assign_task with task_ids only
                        # updates assigned_to, NOT status. Bump it
                        # to "pending" so the task reads as actually
                        # claimed, not still in the pool.
                        await _mcp_call_admin(
                            name, "update_task_status", {
                                "task_id": task_id,
                                "status": "pending",
                            },
                        )
                        result = {
                            "content": [{"type": "text",
                                         "text": f"Claimed task {task_id}: now assigned to {claimant_id}."}],
                            "structuredContent": {"task_id": task_id,
                                                  "claimed_by": claimant_id,
                                                  "status": "pending",
                                                  "title": row.get("title")},
                            "isError": False,
                        }
        elif tool == "update_my_task_status":
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            task_id = (args.get("task_id") or "").strip()
            new_status = (args.get("status") or "").strip()
            notes = args.get("notes")
            valid = {"pending", "in_progress", "completed", "cancelled", "failed"}
            if not task_id or new_status not in valid:
                result = {
                    "content": [{"type": "text",
                                 "text": "Provide 'task_id' and a 'status' "
                                         "in {pending, in_progress, completed, "
                                         "cancelled, failed}."}],
                    "isError": True,
                }
            elif bearer is None:
                result = {
                    "content": [{"type": "text",
                                 "text": "update_my_task_status requires an authenticated session."}],
                    "isError": True,
                }
            else:
                tokens_now = await _agent_token_map(name)
                caller_id = tokens_now.get(bearer, "<unknown>")
                tasks_all = await _fetch_tasks(name)
                row = next((t for t in tasks_all if t.get("task_id") == task_id), None)
                if row is None:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"No task with id {task_id!r}."}],
                        "isError": True,
                    }
                elif caller_id != "Admin" and row.get("assigned_to") != caller_id:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Task {task_id!r} is assigned to "
                                             f"{row.get('assigned_to')!r}, not to you "
                                             f"({caller_id!r}). Only the assignee or "
                                             "Admin can update its status."}],
                        "isError": True,
                    }
                else:
                    upstream_args = {"task_id": task_id, "status": new_status}
                    if notes:
                        upstream_args["notes"] = notes
                    upstream = await _mcp_call_admin(
                        name, "update_task_status", upstream_args,
                    )
                    up_text = "\n".join(
                        p.get("text", "")
                        for p in upstream.get("content", []) or []
                    )
                    if upstream.get("isError") or up_text.lower().startswith("error"):
                        result = {
                            "content": [{"type": "text",
                                         "text": f"Upstream update_task_status refused: {up_text[:300]}"}],
                            "isError": True,
                        }
                    else:
                        result = {
                            "content": [{"type": "text",
                                         "text": f"Task {task_id} status → {new_status}"}],
                            "structuredContent": {"task_id": task_id,
                                                  "status": new_status,
                                                  "updated_by": caller_id},
                            "isError": False,
                        }
        elif tool in ("create_task_for", "create_task_for_self"):
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            title = (args.get("title") or "").strip()
            description = (args.get("description") or "").strip()
            priority = (args.get("priority") or "medium").strip().lower()
            if priority not in ("low", "medium", "high"):
                priority = "medium"
            # Resolve assignee: self → from bearer; delegation → arg.
            if tool == "create_task_for_self":
                target_id = None  # filled in below from bearer
                missing_args = (not title or not description)
                err_msg = "Both 'title' and 'description' are required."
            else:
                target_id = (args.get("agent_id") or "").strip()
                missing_args = (not target_id or not title or not description)
                err_msg = "All of 'agent_id', 'title', 'description' are required."
            # Defense in depth: cross-agent delegation is admin-only.
            # The tools/list rewrite already hides create_task_for from
            # worker sessions; this check refuses a worker who guesses
            # the tool name and POSTs directly.
            admin_only_block: str | None = None
            if tool in _ADMIN_ONLY_SYNTHETIC and bearer is not None:
                tokens_now = await _agent_token_map(name)
                if tokens_now.get(bearer) != "Admin":
                    admin_only_block = (
                        f"{tool} is an admin-only delegation tool. "
                        "Workers should use create_task_for_self for "
                        "themselves, or create_unassigned_task to "
                        "post work into the pool for a peer to claim."
                    )
            if admin_only_block is not None:
                result = {
                    "content": [{"type": "text", "text": admin_only_block}],
                    "isError": True,
                }
            elif missing_args:
                result = {
                    "content": [{"type": "text", "text": err_msg}],
                    "isError": True,
                }
            elif bearer is None:
                result = {
                    "content": [{"type": "text",
                                 "text": f"{tool} requires an authenticated session."}],
                    "isError": True,
                }
            else:
                token_map = await _agent_token_map(name)
                caller_id = token_map.get(bearer, "<unknown>")
                preset_error: str | None = None
                target_token: str | None = None
                if tool == "create_task_for_self":
                    target_id = caller_id
                    target_token = bearer
                elif target_id == caller_id:
                    preset_error = (
                        f"You are {caller_id!r}. To assign work to "
                        "yourself use create_task_for_self."
                    )
                else:
                    for tok, aid in token_map.items():
                        if aid == target_id:
                            target_token = tok
                            break
                # Look up the target agent's token so assign_task can
                # bind the new task to them. agent_token is what
                # upstream uses to identify the assignee.
                token_map = await _agent_token_map(name)
                target_token: str | None = None
                for tok, aid in token_map.items():
                    if aid == target_id:
                        target_token = tok
                        break
                if preset_error is not None:
                    result = {
                        "content": [{"type": "text", "text": preset_error}],
                        "isError": True,
                    }
                elif target_token is None:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Unknown agent_id {target_id!r}. "
                                             "Call list_agents to see valid options."}],
                        "isError": True,
                    }
                else:
                    # Upstream's assign_task ignores agent_token when
                    # called with title+description (creation path).
                    # Do it in two hops: create unassigned, then
                    # re-call assign_task with task_ids + agent_token
                    # to bind the new task to the target.
                    create_resp = await _mcp_call_admin(
                        name, "assign_task", {
                            "task_title": title,
                            "task_description": description,
                            "priority": priority,
                            "auto_suggest_parent": False,
                            "validate_agent_workload": False,
                            "override_rag": True,
                            "override_reason": "router create_assigned_task (step 1)",
                        },
                    )
                    create_text = "\n".join(
                        p.get("text", "")
                        for p in create_resp.get("content", []) or []
                    )
                    m = re.search(r"task_[A-Za-z0-9]+", create_text)
                    task_id = m.group(0) if m else None
                    if create_resp.get("isError") or not task_id:
                        result = {
                            "content": [{"type": "text",
                                         "text": f"Upstream assign_task (create) refused: {create_text[:300]}"}],
                            "isError": True,
                        }
                    else:
                        assign_resp = await _mcp_call_admin(
                            name, "assign_task", {
                                "agent_token": target_token,
                                "task_ids": [task_id],
                                "auto_suggest_parent": False,
                                "validate_agent_workload": False,
                                "override_rag": True,
                                "override_reason": "router create_assigned_task (step 2)",
                            },
                        )
                        assign_text = "\n".join(
                            p.get("text", "")
                            for p in assign_resp.get("content", []) or []
                        )
                        if assign_resp.get("isError"):
                            result = {
                                "content": [{"type": "text",
                                             "text": f"Created {task_id} but assigning to "
                                                     f"{target_id!r} failed: {assign_text[:300]}"}],
                                "isError": True,
                            }
                        else:
                            # Same upstream quirk as claim_task: the
                            # re-assign only updates assigned_to, not
                            # status. Bump status to "pending" so the
                            # task isn't stuck at "unassigned".
                            await _mcp_call_admin(
                                name, "update_task_status", {
                                    "task_id": task_id,
                                    "status": "pending",
                                },
                            )
                            result = {
                                "content": [{"type": "text",
                                             "text": f"Created task {task_id} assigned to {target_id}: {title}"}],
                                "structuredContent": {"task_id": task_id,
                                                      "title": title,
                                                      "priority": priority,
                                                      "status": "pending",
                                                      "assigned_to": target_id},
                                "isError": False,
                            }
        elif tool == "set_project_context":
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            context_key = (args.get("context_key") or "").strip()
            has_value = "context_value" in args
            context_value = args.get("context_value")
            description = (args.get("description") or "").strip()
            if not context_key or not has_value:
                result = {
                    "content": [{"type": "text",
                                 "text": "Both 'context_key' and 'context_value' are required."}],
                    "isError": True,
                }
            elif bearer is None:
                result = {
                    "content": [{"type": "text",
                                 "text": "set_project_context requires an authenticated session."}],
                    "isError": True,
                }
            else:
                upstream_args = {
                    "context_key": context_key,
                    "context_value": context_value,
                }
                if description:
                    upstream_args["description"] = description
                upstream = await _mcp_call_admin(
                    name, "update_project_context", upstream_args,
                )
                up_text = "\n".join(
                    p.get("text", "")
                    for p in upstream.get("content", []) or []
                )
                bad = (
                    upstream.get("isError")
                    or up_text.lower().startswith("error")
                    or "unauthor" in up_text.lower()
                )
                if bad:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Upstream update_project_context refused: {up_text[:300]}"}],
                        "isError": True,
                    }
                else:
                    # Defensive: don't echo back the value (might be
                    # secret-ish for some keys).
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Project context updated: {context_key}"}],
                        "structuredContent": {"context_key": context_key,
                                              "written": True},
                        "isError": False,
                    }
        elif tool == "send_peer_message":
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            recipient = (args.get("recipient_id") or "").strip()
            message = (args.get("message") or "").strip()
            priority = (args.get("priority") or "normal").strip().lower()
            if priority not in ("low", "normal", "high", "urgent"):
                priority = "normal"
            if not recipient or not message:
                result = {
                    "content": [{"type": "text",
                                 "text": "Both 'recipient_id' and 'message' are required."}],
                    "isError": True,
                }
            elif bearer is None:
                result = {
                    "content": [{"type": "text",
                                 "text": "send_peer_message requires an authenticated session."}],
                    "isError": True,
                }
            else:
                tokens = await _agent_token_map(name)
                sender_id = tokens.get(bearer, "<unknown>")
                # Prepend the sender so the recipient knows who it's
                # from — upstream's send_agent_message uses the auth
                # token's identity, so the relayed message would
                # otherwise look like "from Admin".
                relayed = f"[from {sender_id}] {message}"
                upstream = await _mcp_call_admin(
                    name, "send_agent_message", {
                        "recipient_id": recipient,
                        "message": relayed,
                        "priority": priority,
                        "deliver_method": "store",
                    },
                )
                up_text = "\n".join(
                    p.get("text", "")
                    for p in upstream.get("content", []) or []
                )
                # Upstream returns isError=false even on auth/policy
                # denials, so check the text payload too.
                bad = (
                    upstream.get("isError")
                    or "denied" in up_text.lower()
                    or "not permitted" in up_text.lower()
                    or up_text.lower().startswith("error")
                )
                if bad:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Relay refused: {up_text[:300]}"}],
                        "isError": True,
                    }
                else:
                    result = {
                        "content": [{"type": "text",
                                     "text": f"Message relayed to {recipient!r} from {sender_id!r}."}],
                        "structuredContent": {"recipient_id": recipient,
                                              "sender_id": sender_id,
                                              "delivered": True},
                        "isError": False,
                    }
        elif tool == "list_tasks_for":
            args = params.get("arguments") or {}
            target = (args.get("agent_id") or "").strip() if isinstance(args, dict) else ""
            if not target:
                result = {
                    "content": [{"type": "text",
                                 "text": "Missing required argument 'agent_id'."}],
                    "isError": True,
                }
            else:
                tasks_all = await _fetch_tasks(name)
                tasks = [
                    t for t in tasks_all
                    if (t.get("assigned_to") or "") == target
                ]
                if not tasks:
                    text = f"No tasks assigned to {target!r}."
                else:
                    lines = [
                        f"tasks assigned to {target!r} on project {name!r}:"
                    ]
                    for t in tasks:
                        desc = (t.get("description") or "")
                        preview = (desc[:80] + "…") if len(desc) > 80 else desc
                        preview = preview.replace("\n", " ")
                        lines.append(
                            f"  {t.get('task_id','?')}  "
                            f"[{t.get('status','?')}/{t.get('priority','?')}]  "
                            f"{t.get('title','?')}"
                        )
                        if preview:
                            lines.append(f"      {preview}")
                    text = "\n".join(lines)
                result = {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {"agent_id": target, "tasks": tasks},
                    "isError": False,
                }
        else:
            result = {
                "content": [
                    {"type": "text", "text": f"Unknown synthetic tool: {tool}"}
                ],
                "isError": True,
            }
    except Exception as e:
        result = {
            "content": [
                {"type": "text", "text": f"Synthetic tool {tool!r} failed: {e!r}"}
            ],
            "isError": True,
        }
    response = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    await queue.put(_format_sse_event(response))
    return True


async def backend_messages_handler(req: web.Request) -> web.StreamResponse:
    """/agent-mcp/__messages/<name>/{rest} → backend /messages/{rest}

    If `Authorization: Bearer <token>` is present and the body is
    a JSON-RPC tools/call, inject the bearer into
    `params.arguments.token` (overwriting any model-supplied
    value). Other JSON-RPC methods and unauthenticated requests
    are passed through unchanged.

    tools/call for router-synthesised tool names (e.g. list_agents)
    is short-circuited: the response is built here and pushed to
    the SSE pump's inject queue; the POST returns 202 without
    forwarding upstream.
    """
    name = req.match_info["name"]
    rest = req.match_info.get("rest", "")
    bearer = _extract_bearer(req)
    body_override: bytes | None = None

    # First pass: peek at the body without committing to a flow.
    # We want to intercept router-synthesised tool calls *regardless*
    # of whether the session is bearer-authenticated, so the handler
    # can return an appropriate isError response (e.g. claim_task
    # without a bearer can't know who the claimant is).
    raw_peek: bytes | None = None
    try:
        raw_peek = await req.read()
    except Exception:
        raw_peek = None
    if raw_peek is not None:
        try:
            payload = json.loads(raw_peek.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("method") == "tools/call"
            and isinstance(payload.get("params"), dict)
            and payload["params"].get("name") in _SYNTHETIC_TOOL_NAMES
        ):
            sid = req.rel_url.query.get("session_id", "")
            handled = await _handle_synthetic_tool_call(
                name, sid, payload, bearer=bearer,
            )
            if handled:
                return web.Response(
                    text="Accepted", status=202,
                    headers={"Cache-Control": "no-store"},
                )
        # Not a synthetic tool call — fall through to the regular
        # path, using the bytes we already read as body_override.
        body_override = raw_peek

    if bearer is not None:
        # Auth pre-check: 401 bad tokens at the router edge rather
        # than letting the backend reject downstream.
        tokens = await _agent_token_map(name)
        if bearer not in tokens:
            raise _unauthorized()
        # NOTE: body-rewrite injection of `arguments.token` was removed
        # — the fork's AuthHeaderMiddleware (dvaerum/Agent-MCP#19)
        # reads `Authorization: Bearer` into a ContextVar and the tool
        # dispatcher injects from there when `arguments.token` is
        # missing. We forward the Authorization header to the backend
        # (see _proxy_to_backend) and let it do the work.
    return await _proxy_to_backend(
        req, name, f"/messages/{rest}", body_override=body_override
    )


async def backend_api_handler(req: web.Request) -> web.StreamResponse:
    """/agent-mcp/__api/<name>/{rest} → backend /api/{rest}"""
    rest = req.match_info.get("rest", "")
    return await _proxy_to_backend(
        req, req.match_info["name"], f"/api/{rest}"
    )


# ── MCP bridge (dashboard ↔ tools/call) ──────────────────────────────
#
# The upstream dashboard's "Create Task" button POSTs to /api/tasks,
# but the backend exposes that path GET-only — POST 405s. The dashboard
# never actually worked. We implement a thin REST shim here that
# translates the dashboard's POST into an MCP `tools/call assign_task`
# over a one-shot SSE session, using the backend's own admin token
# (which we fetch via the existing GET /api/tokens). The dashboard
# never sees the token.


async def _mcp_call_admin(
    name: str, tool: str, arguments: dict, *, timeout: float = 20
) -> dict:
    """Open an SSE session as Admin and issue a single tools/call.

    Returns the raw `result` dict from the JSON-RPC response. Raises
    on transport/initialise failure so the caller can map to a 5xx.
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


# Removed: bridge_call_handler + bridge_create_task_handler.
# The dashboard now calls upstream REST endpoints directly:
#   - POST /api/tasks   (dvaerum/Agent-MCP#12)
#   - DELETE /api/tasks/<id> (dvaerum/Agent-MCP#12)
#   - POST /api/update-task-dashboard (existing upstream)
# Wiring landed in dvaerum/Agent-MCP#22. _mcp_call_admin is kept for
# the synthetic-tool path (list_agents et al. still aggregate over
# backend REST as Admin); to be revisited when those retire.


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
        {"projects": sorted(_read_projects().keys())},
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

    projects = _read_projects()
    err = _validate_name(name, projects)
    if err is not None:
        raise web.HTTPBadRequest(reason=err)

    workspace = (DEFAULT_WORKSPACE_PARENT / name).expanduser().resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise web.HTTPBadRequest(
            reason=f"could not create workspace {workspace}: {e.strerror}"
        )

    projects[name] = str(workspace)
    _write_projects(projects)

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
    if name not in _read_projects():
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

    projects = _read_projects()
    if name not in projects:
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    projects.pop(name)
    _write_projects(projects)

    _systemctl("stop", _unit_name(name, "backend"))

    raise web.HTTPSeeOther(location="/agent-mcp/")


# ── Wiring helpers (client-config / installer) ───────────────────────


def _sse_url_for(name: str) -> str:
    return f"{EXTERNAL_URL}/agent-mcp/__sse/{name}"


def _mcp_json_for(name: str, *, token: str | None = None) -> dict:
    entry: dict = {"type": "sse", "url": _sse_url_for(name)}
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
        .replace("__AGENT_MCP_SSE_URL__", _sse_url_for(name))
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
    if name not in _read_projects():
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
    if name not in _read_projects():
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
    if name not in _read_projects():
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
    for name, path in sorted(_read_projects().items()):
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

    sse = _sse_url_for(name)
    auth_flag = (
        f' --header "Authorization: Bearer {sel_token}"' if sel_token else ""
    )
    cli_cmd = f"claude mcp add --transport sse agent-mcp {sse}{auth_flag}"
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
  <p><strong>SSE URL:</strong> <code>{escape(sse)}</code></p>
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
    projects = _read_projects()
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


def make_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(reconcile_on_startup)
    app.on_startup.append(_start_reaper_task)
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
    app.router.add_get("/agent-mcp/__dashboard/{name}/", dashboard_handler)
    app.router.add_get(
        "/agent-mcp/__dashboard/{name}/{rest:.*}", dashboard_handler
    )

    # Backend operations — three distinct routes that all proxy to
    # the project's UDS, but with different rewritten paths because
    # agent-mcp itself expects them at the root (/sse, /messages/…,
    # /api/…).
    app.router.add_route("*", "/agent-mcp/__sse/{name}", backend_sse_handler)
    app.router.add_route(
        "*", "/agent-mcp/__messages/{name}/{rest:.*}", backend_messages_handler
    )
    app.router.add_route(
        "*", "/agent-mcp/__api/{name}/{rest:.*}", backend_api_handler
    )

    # __bridge routes removed: dashboard now uses upstream REST
    # endpoints directly (dvaerum/Agent-MCP#12 + #22).

    return app


def main() -> None:
    # AGENT_MCP_ROUTER_HOST lets the VM module bind 0.0.0.0 so qemu's
    # user-mode hostfwd can deliver packets in. The home-manager
    # deployment leaves it unset → 127.0.0.1, the upstream default.
    host = os.environ.get("AGENT_MCP_ROUTER_HOST", "127.0.0.1")
    web.run_app(make_app(), host=host, port=ROUTER_PORT)


if __name__ == "__main__":
    main()
