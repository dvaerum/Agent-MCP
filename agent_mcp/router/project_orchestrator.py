"""Per-project lifecycle orchestrator for the router.

PR-C of the round-2 architecture review (improve-codebase-architecture
report 20260611): the per-project state machine — systemd activation,
the idle reaper, the alias-grace reaper, startup reconciliation, and
alias resolution — was extracted out of ``agent_mcp.router.app`` into
this sibling module.

Why split: ``router/app.py`` had grown to ~2855 lines and was carrying
three different concerns: (1) URL dispatch + the catch-all reverse
proxy, (2) the dashboard ops surface (overview JSON, project
lifecycle REST), and (3) the lazy backend state machine + background
loops. Concerns (1) and (2) want to live in a thin aiohttp app
that's easy to test by hitting URLs; concern (3) wants to be a
typed object you can construct against a registry and a stubbed
``_systemctl`` and exercise without spinning up an HTTP server.

ADR-0009 is explicit: the dashboard owns the ops surface — i.e. the
``/agent-mcp/__overview`` JSON envelope, the REST project-lifecycle
handlers, and the legacy form-encoded HTML-redirect handlers all
stay in ``router/app.py``. This module hosts the state machine those
handlers delegate to (``ProjectOrchestrator.start``, ``stop``,
``resolve``, etc.), not the HTTP surfaces themselves.

ADR-0010 is the contract for the alias-with-grace half: an alias
whose ``expires_at`` is in the past is removed by
``alias_expiry_tick``; an alias with a malformed timestamp is
preserved so the operator can clean up by hand (we don't silently
drop data we can't reason about).

ADR-0008 is satisfied trivially — the orchestrator is mode-agnostic;
single-tenant N=1 uses the same code path as multi-tenant, the
``make_app`` in ``router/app.py`` is the only place that branches on
``SINGLE_TENANT_NAME``.

Shared module-level state lives HERE; ``router/app.py`` re-exports
``last_active``, ``active_conns``, ``ensure_locks``, ``IDLE_SEC``,
``_systemctl``, ``_ensure``, ``reaper``, etc. so the existing test
surface (``router_module.last_active``, ``router_module._systemctl``,
…) keeps working without churn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from . import project_registry


log = logging.getLogger(__name__)


# ── Configuration (mirrors router/app.py) ───────────────────────────
# Read at import time. The orchestrator is imported BY ``router/app.py``
# so the env-var read here happens once per router process.
SOCK_DIR = Path(os.environ["AGENT_MCP_SOCK_DIR"])
IDLE_SEC = int(os.environ.get("AGENT_MCP_IDLE_SEC", str(4 * 60 * 60)))


# ── Shared module-level state ───────────────────────────────────────
# These were ``router.app`` globals before PR-C; they now live here
# and ``router/app.py`` re-exports the names so existing tests and
# call sites (e.g. ``router_module.last_active``,
# ``router_module._systemctl``) keep working without modification.

# Activity timestamps per (name, role). role is always "backend"
# today (the dashboard is static, served by the router itself), but
# the dict shape is kept tuple-keyed so a future sidecar role
# drops in cleanly.
last_active: dict[tuple[str, str], float] = {}

# Per-project in-flight connection counter. Incremented when a proxied
# request enters ``_proxy_to_backend`` (router/app.py), decremented
# when it leaves. ``stop()`` refuses to act while the counter is
# non-zero so an SSE session isn't yanked mid-stream.
active_conns: dict[str, int] = defaultdict(int)

# Per-(name, role) lock serialising ``_ensure``. The dashboard fires
# several parallel API calls on first load; without this each one
# raced systemctl independently — fastest wins, the rest see the unit
# in a transient state and issue a ``restart``, causing a stop/start
# storm and a ~10 s window where requests 504.
ensure_locks: dict[tuple[str, str], asyncio.Lock] = {}

# Recent-failure cache for ``_ensure`` (P005 cascade-fix, 2026-06-19).
# Maps (name, role) → (monotonic_failed_at, reason). When ``_ensure``
# fails to bring the backend's UDS up within the socket-wait budget,
# we record the failure here. Subsequent calls within
# ``ENSURE_FAILURE_COOLDOWN_SEC`` short-circuit with the same 504
# instead of paying another full socket-wait. Without this cap, a
# dashboard's first-paint fan-out (6 parallel reads, see the lock
# comment above) serialises through ``ensure_locks`` and pays N × 20 s
# behind a backend that's failing to come up — every per-project fetch
# aborts client-side at 30 s and the page renders empty.
#
# Cooldown window is intentionally short so transient
# systemctl-races recover quickly. The reaper (`_reaper_tick`) and
# any successful `_ensure` evict the entry; tests can patch the
# constant or call `_clear_ensure_failures()` to reset between
# scenarios.
ensure_failures: dict[tuple[str, str], tuple[float, str]] = {}

# How long a failed ``_ensure`` stays cached. Read at every call so
# tests can monkeypatch the module-level value mid-test without
# re-importing.
ENSURE_FAILURE_COOLDOWN_SEC: float = float(
    os.environ.get("AGENT_MCP_ENSURE_FAILURE_COOLDOWN_SEC", "5")
)


def _clear_ensure_failures() -> None:
    """Drop every cached ``_ensure`` failure. Test helper.

    Production callers don't need this — the entry is auto-evicted on
    the next successful ``_ensure`` or after the cooldown window
    expires. Tests reset between scenarios.
    """
    ensure_failures.clear()


# ── Backend lifecycle primitives ────────────────────────────────────


def _sock_path(name: str, role: str) -> Path:
    d = SOCK_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{role}.sock"


# ── Forwarding-header HMAC key (retire-system-token Wave 2) ────────
# The router signs an ``X-Agent-MCP-Forwarded-Operator`` header on
# every cookie-authenticated request that proxies to a per-project
# backend. Backend's ``AuthHeaderMiddleware`` (Wave 1) verifies the
# signature against the same HMAC key, loaded into
# ``g.forwarding_hmac_key`` at backend bootstrap via the
# ``--forwarding-hmac-in`` flag.
#
# The router OWNS this key: generated at backend spawn time, written
# to ``<sock_dir>/<name>/forwarding_hmac`` (mode 0600) so the
# launcher can hand the path to the backend, and stashed in this
# in-memory map so signing on every request doesn't pay a disk read.
#
# The dict is cleared per-(name) on backend stop so a project that's
# re-spawned after a workspace teardown rotates the key cleanly.

# {project_name: HMAC key bytes (32 bytes of os.urandom)}
forwarding_hmac_keys: dict[str, bytes] = {}


def _forwarding_hmac_path(name: str) -> Path:
    """Path the router writes the per-project HMAC key to.

    Same directory as the backend's UDS so the launcher's
    ``$AGENT_MCP_SOCK_DIR/$name/`` mkdir covers both files in one
    step and a project teardown drops the key alongside the socket
    — no stale key file surviving a backend rebuild.
    """
    d = SOCK_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d / "forwarding_hmac"


def ensure_forwarding_hmac_key(name: str) -> bytes:
    """Return the per-project HMAC key, generating + persisting if needed.

    Idempotent: if a key file already exists on disk (e.g. the router
    crashed mid-spawn and restarted, or a stale file is left over from
    a previous backend instance), the on-disk value wins so the
    already-spawned backend's loaded key stays compatible. The in-
    memory cache is populated on first use so subsequent calls within
    the same router process are pure dict lookups.

    The file is written with mode 0600 (router-only readable). The
    launcher passes the path to the backend via
    ``--forwarding-hmac-in``; the backend reads it once at startup
    into ``g.forwarding_hmac_key``.

    Called eagerly from ``_ensure`` BEFORE the systemctl start so the
    file is on disk by the time the backend's bootstrap reads it.

    Self-healing on cache hit (F015 v3): if the in-memory cache has
    the key but the on-disk file is gone, re-write the cached bytes.
    The systemd unit declares ``RuntimeDirectory=agent-mcp/%i`` with
    ``RuntimeDirectoryPreserve=no`` (the systemd default), so every
    ``systemctl stop`` wipes ``/run/agent-mcp/<name>/`` — including
    this key file. When the idle reaper stops a backend without
    popping the cache, the next spawn would call us, see the cache
    hit, return the bytes, and let ``_ensure`` invoke ``systemctl
    start``; the backend launcher then exits 2 because click's
    ``--forwarding-hmac-in File()`` validator can't find the file.
    The self-heal branch re-creates the file with the same bytes so
    the spawn succeeds. We do NOT rotate here — any backend already
    loaded with the cached key would fail HMAC verify against a new
    one.
    """
    path = _forwarding_hmac_path(name)
    cached = forwarding_hmac_keys.get(name)
    if cached is not None:
        if not path.exists():
            # Out-of-band deletion (most commonly systemd
            # RuntimeDirectory teardown between stop + restart).
            # Re-write the cached bytes; mode 0600 to match the
            # generate branch's invariant.
            fd = os.open(
                str(path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            try:
                os.write(fd, cached)
            finally:
                os.close(fd)
        return cached
    # If a key file already exists on disk, adopt it. Defends the
    # router-restart scenario: a backend that's already running was
    # spawned with whatever key the previous router process wrote —
    # generating a new key here would silently break HMAC verification
    # for every cookie-authenticated request until the backend is
    # restarted. The router-restart case (orphaned backend with a
    # known key file) is handled by ``reconcile_on_startup``'s
    # adoption pass; this branch covers the in-flight readers.
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    except OSError as exc:  # pragma: no cover - defensive
        log.warning(
            "Failed to read existing HMAC key for project %r at %s: %s",
            name, path, exc,
        )
        existing = None
    if existing:
        forwarding_hmac_keys[name] = existing
        return existing
    # Fresh spawn — generate 32 random bytes and persist.
    key = secrets.token_bytes(32)
    # Atomic-ish write: open with O_CREAT|O_EXCL|O_TRUNC + 0600 so a
    # racing reader never sees a half-written key. We don't need a
    # tmpfile + rename here because the file is single-writer (the
    # router process) and the launcher always reads AFTER the
    # systemctl start completes (sequenced via ``_ensure``).
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    forwarding_hmac_keys[name] = key
    return key


def get_forwarding_hmac_key(name: str) -> bytes | None:
    """Return the cached per-project HMAC key, or None if not set.

    Read-only: never generates a new key. Used by the router's
    ``_forwarding_header_from_cookie`` path; if the key isn't loaded
    yet (e.g. the cookie request raced backend spawn), the cookie
    path returns None and the caller 401s.
    """
    cached = forwarding_hmac_keys.get(name)
    if cached is not None:
        return cached
    # Fallback: read off disk in case ``_ensure`` ran in a different
    # process or before the in-memory cache was populated. This
    # mirrors ``ensure_forwarding_hmac_key``'s adopt-existing branch
    # but never writes a new key.
    try:
        existing = _forwarding_hmac_path(name).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:  # pragma: no cover - defensive
        return None
    if existing:
        forwarding_hmac_keys[name] = existing
        return existing
    return None


def _unit_name(name: str, role: str) -> str:
    if role == "backend":
        return f"agent-mcp@{name}.service"
    raise ValueError(f"unsupported role: {role!r}")


# Whether to call ``systemctl --user`` (default, matches the
# nixos-developer-system / home-manager deployment) or plain
# ``systemctl`` (system mode, used by the in-VM flake deployment
# where the router runs as a root system service with no D-Bus
# session bus available). Reads ``AGENT_MCP_SYSTEMCTL_MODE`` env
# var; preserved from the pre-upstream vendored router (PR #159
# missed this env-var check during extraction; surfaced by VM e2e
# 2026-06-16 — every /agent-mcp/api/<project>/... 500'd with
# "Failed to connect to user scope bus").
_SYSTEMCTL_MODE = os.environ.get(
    "AGENT_MCP_SYSTEMCTL_MODE", "user"
).strip().lower()


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    base = ["systemctl"]
    if _SYSTEMCTL_MODE == "user":
        base.append("--user")
    return subprocess.run(
        [*base, *args], capture_output=True, text=True
    )


def _is_active(unit: str) -> bool:
    return _systemctl("is-active", unit).returncode == 0


def _ensure_lock(name: str, role: str) -> asyncio.Lock:
    key = (name, role)
    lock = ensure_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        ensure_locks[key] = lock
    return lock


@contextlib.asynccontextmanager
async def _track_connection(name: str):
    """Per-project in-flight connection counter context manager.

    ``stop()`` and the REST lifecycle handlers in ``router/app.py``
    refuse to act on a project whose counter is non-zero, so this is
    the one observability point that gates lifecycle operations
    behind active client traffic.
    """
    active_conns[name] += 1
    try:
        yield
    finally:
        active_conns[name] -= 1
        if active_conns[name] <= 0:
            active_conns.pop(name, None)


async def _ensure(name: str, role: str) -> Path:
    """Make sure the backend for (name, role) is running; return its sock.

    "Running" requires both ``is-active`` AND the socket file existing;
    the backend's systemd unit can stay "active" while the socket has
    gone stale (e.g. after a backend crash mid-write). In that case
    we ``restart``, not ``start``.

    Serialised per (name, role) so a burst of parallel requests (e.g.
    the dashboard's first-paint fan-out of /status /agents /tasks
    /graph-data) only triggers one systemctl invocation.

    Uses the module-level registry handle from ``project_registry`` —
    the router's ``_REGISTRY`` and the orchestrator's
    ``ProjectOrchestrator.registry`` are the same object in production
    so the lookup here matches what the lifecycle handlers see.
    """
    # Look the project up via the module-level registry. Tests patch
    # ``project_registry.REGISTRY_PATH`` before importing the
    # orchestrator, so a fresh ``ProjectRegistry()`` here picks up the
    # test path.
    registry = project_registry.ProjectRegistry()
    if registry.get(name) is None:
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    unit = _unit_name(name, role)
    sock = _sock_path(name, role)

    async with _ensure_lock(name, role):
        needs_start = not _is_active(unit) or not sock.exists()
        # Recent-failure short-circuit (P005 cascade-fix). If the
        # previous ``_ensure`` for this (name, role) raised within
        # ``ENSURE_FAILURE_COOLDOWN_SEC`` AND the backend STILL isn't
        # ready, re-raise the same 504 immediately instead of paying
        # another full socket-wait. Without this, a dashboard's
        # 6-request first-paint fan-out serialises through
        # ``_ensure_lock`` and each queued caller pays a fresh 20 s
        # wait behind a backend that's failing to come up — every
        # per-project fetch aborts client-side at 30 s and the page
        # renders empty.
        #
        # The check sits AFTER the freshness probe (``needs_start``)
        # so a backend that recovered between the cached failure and
        # this call (e.g. the operator restarted the systemd unit
        # manually) does NOT inherit a phantom 504 for the rest of
        # the cooldown window — the next caller observes the unit
        # active + the socket present and falls through to the
        # success path.
        if needs_start:
            fail_entry = ensure_failures.get((name, role))
            if fail_entry is not None:
                failed_at, reason = fail_entry
                if time.monotonic() - failed_at < ENSURE_FAILURE_COOLDOWN_SEC:
                    raise web.HTTPGatewayTimeout(reason=reason)
                # Cooldown elapsed — drop the stale entry so we retry.
                ensure_failures.pop((name, role), None)
            # retire-system-token Wave 2: ensure the forwarding-header
            # HMAC key file exists BEFORE the backend starts. The
            # backend's ``--forwarding-hmac-in`` bootstrap reads the
            # file at startup; if it isn't present yet, the cookie
            # path silently degrades to "key not loaded" (Wave 1
            # dormant fallback) and every cookie request 401s. Sits
            # inside ``_ensure_lock`` so concurrent ``_ensure`` calls
            # don't race on file creation.
            ensure_forwarding_hmac_key(name)
            action = "restart" if _is_active(unit) else "start"
            r = _systemctl(action, unit)
            if r.returncode != 0:
                reason = (
                    f"systemctl {action} {unit} failed: "
                    f"{r.stderr.strip()}"
                )
                # Same cooldown applies to a hard systemctl failure
                # — without it, the next queued request immediately
                # invokes systemctl again, which loops on the same
                # unit-file / permission / OOM condition.
                ensure_failures[(name, role)] = (time.monotonic(), reason)
                raise web.HTTPInternalServerError(reason=reason)
            # Poll for the socket file. Production waits up to ~20 s
            # (200 × 0.1 s) for the unit to come up. The budget is
            # env-overridable so unit tests — which stub systemctl and
            # never spawn a real backend — can set
            # AGENT_MCP_ENSURE_SOCKET_ATTEMPTS=1 and get the not-ready
            # 504 in ~0.1 s instead of stalling 20 s per test.
            attempts = int(
                os.environ.get("AGENT_MCP_ENSURE_SOCKET_ATTEMPTS", "200")
            )
            for _ in range(attempts):  # ≤ attempts × 0.1 s
                if sock.exists() and sock.is_socket():
                    break
                await asyncio.sleep(0.1)
            else:
                reason = (
                    f"{unit} did not create {sock} within "
                    f"~{attempts * 0.1:.0f} s"
                )
                ensure_failures[(name, role)] = (time.monotonic(), reason)
                raise web.HTTPGatewayTimeout(reason=reason)
        # Success — evict any stale failure entry so the next caller
        # doesn't see a phantom cooldown for a now-healthy backend.
        ensure_failures.pop((name, role), None)
        last_active[(name, role)] = time.time()
    return sock


# ── Background loops ────────────────────────────────────────────────


async def reaper(app: web.Application | None = None) -> None:
    """Idle-backend reaper. Stops systemd units whose last activity
    is older than ``IDLE_SEC``.

    Runs as a long-lived task; one tick per 60 s. The body is
    extracted into ``_reaper_tick`` so tests and the orchestrator's
    public ``reaper_tick()`` method can drive a single deterministic
    scan without sleeping a minute first.
    """
    while True:
        await asyncio.sleep(60)
        await _reaper_tick()


async def _reaper_tick() -> None:
    """One pass of the idle-reaper logic. ``stop``s every unit whose
    last-active timestamp is older than ``IDLE_SEC``.

    Reads the module-level ``IDLE_SEC`` rather than capturing it at
    function-definition time so tests that monkeypatch it after
    import see their override reflected.
    """
    now = time.time()
    for key in list(last_active.keys()):
        if now - last_active[key] > IDLE_SEC:
            _systemctl("stop", _unit_name(*key))
            last_active.pop(key, None)
            # Cache symmetry with ``ProjectOrchestrator.stop()`` (see
            # the ``forwarding_hmac_keys.pop`` at line ~648). Without
            # this, the reaper-driven stop path leaves the in-memory
            # HMAC cache populated while systemd's RuntimeDirectory
            # teardown wipes the on-disk key file. The next spawn
            # then either (a) reuses a key the backend can no longer
            # produce on disk — caught by the self-heal branch in
            # ``ensure_forwarding_hmac_key`` — or (b) rotates cleanly
            # if we pop here. We prefer (b): cache symmetry between
            # the REST stop path and the reaper stop path keeps the
            # two paths reasoning-equivalent.
            name, role = key
            if role == "backend":
                forwarding_hmac_keys.pop(name, None)


# ── Alias-grace reaper (ADR-0010) ───────────────────────────────────


# Cadence between alias-reaper ticks. The grace period for aliases is
# measured in days (default 30), so a 60 s tick gives near-instant
# cleanup of past-due aliases without paying for a tighter loop. Held
# at module scope so tests can monkeypatch to a smaller value.
_ALIAS_REAPER_INTERVAL_SEC = 60


async def _alias_reaper_tick(
    registry: project_registry.ProjectRegistry,
) -> None:
    """Single pass over the registry, removing any alias whose
    ``expires_at`` is in the past. Emits one INFO log line per removal.

    ADR-0010 (alias-with-grace): an alias whose ``expires_at`` is in
    the past must be removed; one whose expiry is in the future
    survives the sweep; one with a *malformed* ``expires_at`` is
    preserved so the operator can clean up by hand. We do not
    silently drop data we can't reason about.

    Extracted from the loop wrapper so tests can call it directly
    with a deterministic registry instance.
    """
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
                # Malformed entry — leave it (ADR-0010); the operator
                # cleans up by hand and we don't want to silently
                # drop data we can't reason about.
                continue
            if exp <= now:
                registry.expire_alias(name, alias_name)
                log.info(
                    "Alias %r for project %r expired and was removed.",
                    alias_name, name,
                )


async def alias_reaper(app: web.Application | None = None) -> None:
    """Long-running task: every ``_ALIAS_REAPER_INTERVAL_SEC`` seconds,
    sweep the registry for aliases whose grace period has lapsed.

    Idempotent w.r.t. concurrent ``rename`` / ``add_alias`` —
    ``expire_alias`` is a write-locked registry operation.

    Reads the module-level registry on each tick rather than capturing
    a handle at start time so tests that override
    ``project_registry.REGISTRY_PATH`` mid-test see their pointer
    reflected.
    """
    while True:
        await asyncio.sleep(_ALIAS_REAPER_INTERVAL_SEC)
        try:
            await _alias_reaper_tick(project_registry.ProjectRegistry())
        except Exception:
            log.exception("alias reaper tick failed; will retry next pass")


# ── Startup reconciliation ──────────────────────────────────────────


async def reconcile_on_startup(app: web.Application | None = None) -> None:
    """Adopt already-running backend units after a router restart.

    Without this, a router crash + restart would orphan every active
    backend — the units stay up (systemd owns them) but the router
    has no ``last_active`` entry for them, so the reaper never
    considers them for idle timeout.

    Reads the live ``systemctl --user list-units`` output and seeds
    ``last_active`` with ``now`` for every active ``agent-mcp@….service``
    unit. The reaper's idle window starts from this seed; the worst
    case is that an actually-idle backend survives one extra IDLE_SEC
    window after a router restart, which is benign.
    """
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


# ── Public orchestrator class ───────────────────────────────────────


class ProjectOrchestrator:
    """Owns per-project lifecycle decisions.

    Pinned methods (RED test contract, ``tests/router/test_project_orchestrator.py``):

      * ``start(name)`` — lazy systemd activation + idempotency. Returns
        the backend's UDS path.
      * ``stop(name, force=False)`` — graceful refusal on active
        connections; ``force=True`` overrides.
      * ``list_active()`` — snapshot of ``last_active`` as a list of
        ``{name, role, last_activity_ts}`` rows.
      * ``resolve(name)`` — ``(real_name, alias_entry_or_None)`` for the
        URL segment ``name``. Raises ``HTTPNotFound`` for unknown.
      * ``add_alias(name, alias, grace_days=…)`` — conflict-detecting
        wrapper around ``registry.add_alias`` that returns a structured
        result dict instead of raising.
      * ``remove_alias(name, alias)`` — idempotent skip-grace removal.
      * ``reaper_tick()`` / ``alias_expiry_tick()`` — single ticks of
        the two background loops; useful in tests.
      * ``reconcile_on_startup()`` — adopt active systemd units.

    The orchestrator is constructed against a ``ProjectRegistry``; in
    production this is the same module-level handle the router uses
    (``router.app._REGISTRY``), so registry mutations made via the
    HTTP lifecycle handlers are immediately visible here. In tests
    the orchestrator gets its own registry pointed at a tmp file.
    """

    def __init__(self, registry: project_registry.ProjectRegistry) -> None:
        self.registry = registry

    # ── start / stop ──────────────────────────────────────────────

    async def start(self, name: str) -> Path:
        """Lazily activate the backend for ``name`` and return its UDS path.

        Wraps the module-level ``_ensure`` helper so the same lock
        ordering, socket-existence retry, and ``last_active`` write
        as the proxy handlers are used. Idempotent: a second call
        with the unit already active and the socket present is a
        no-op (no extra ``systemctl start`` invocation).
        """
        return await _ensure(name, "backend")

    def stop(self, name: str, *, force: bool = False) -> dict:
        """Stop the backend for ``name``.

        Refuses with ``{stopped: False, reason: "active_sessions",
        active_connections: <n>}`` when ``active_conns[name] > 0``
        and ``force`` is False. ``force=True`` runs the systemctl
        stop regardless (used by the operator-confirm path in the
        dashboard's two-tier remove modal).

        Returns a dict so the caller (REST handler, dashboard ops
        button) gets structured fields it can render directly.
        """
        conns = active_conns.get(name, 0)
        if conns > 0 and not force:
            return {
                "stopped": False,
                "reason": "active_sessions",
                "active_connections": conns,
            }
        unit = _unit_name(name, "backend")
        if _is_active(unit):
            r = _systemctl("stop", unit)
            if r.returncode != 0:
                return {
                    "stopped": False,
                    "reason": "systemctl_failed",
                    "message": r.stderr.strip(),
                }
        last_active.pop((name, "backend"), None)
        # Drop the in-memory HMAC key so the next spawn rotates. The
        # file on disk is left in place — the next ``_ensure`` will
        # adopt it via ``ensure_forwarding_hmac_key`` (idempotent
        # against the existing file), so an in-flight cookie request
        # racing the stop+restart cycle still verifies. Operators
        # who want a hard rotation delete the project's sock dir.
        forwarding_hmac_keys.pop(name, None)
        return {"stopped": True}

    # ── list_active ───────────────────────────────────────────────

    def list_active(self) -> list[dict]:
        """Snapshot of ``last_active`` as a list of typed rows.

        Returns a list of ``{name, role, last_activity_ts}`` dicts —
        cheap to JSON-serialise, stable shape for the overview
        endpoint's per-project status panel.
        """
        return [
            {"name": name, "role": role, "last_activity_ts": ts}
            for (name, role), ts in last_active.items()
        ]

    # ── alias resolution ──────────────────────────────────────────

    def resolve(self, name: str) -> tuple[str, dict | None]:
        """Return ``(real_name, alias_entry)`` for the URL segment.

        ``alias_entry`` is None if ``name`` is itself a real project,
        or the matching ``{"name", "expires_at"}`` alias entry if
        ``name`` is a grace-period alias of some other project. Raises
        ``HTTPNotFound`` if neither resolution succeeds.

        Used by the router's MCP + REST proxy handlers to support
        ADR-0010 alias-with-grace: requests to an alias URL are
        transparently re-pointed at the backend for the real project,
        with a sentinel header injected so the backend can later
        surface the deprecation warning to clients.
        """
        row = self.registry.get(name)
        if row is not None:
            return name, None
        real_name = self.registry.resolve_alias(name)
        if real_name is None:
            raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
        real_row = self.registry.get(real_name)
        alias_entry: dict | None = None
        if real_row is not None:
            for entry in real_row.get("aliases", []):
                if entry.get("name") == name:
                    alias_entry = entry
                    break
        return real_name, alias_entry

    # ── alias add / remove ────────────────────────────────────────

    def add_alias(
        self,
        name: str,
        alias: str,
        *,
        grace_days: int = project_registry.DEFAULT_ALIAS_GRACE_DAYS,
    ) -> dict:
        """Add ``alias`` to project ``name`` with conflict detection.

        Discriminator results:

          * ``{ok: True, expires_at: <iso>}`` on success.
          * ``{ok: False, error: "name_taken"}`` when ``alias`` is the
            real name of some other project.
          * ``{ok: False, error: "alias_collision"}`` when ``alias`` is
            already a *currently active* alias of some other project.
          * ``{ok: False, error: "invalid"}`` for slug failures or an
            unknown project ``name``.

        Mirrors the unified-envelope shape used by the REST lifecycle
        handlers in ``router/app.py`` (see ``_error_envelope``), so a
        thin REST handler can wrap this with a single status-code
        mapping rather than re-checking conditions.
        """
        # Real-name collision check — the registry would also catch
        # this via the now-conditional check (only when the alias
        # would be active), but we want the discriminator here even
        # for grace_days=0 callers in the lifecycle handler.
        if self.registry.get(alias) is not None:
            return {"ok": False, "error": "name_taken"}
        if self.registry.resolve_alias(alias) is not None:
            return {"ok": False, "error": "alias_collision"}
        try:
            self.registry.add_alias(name, alias, grace_days=grace_days)
        except KeyError:
            return {"ok": False, "error": "invalid"}
        except ValueError as e:
            return {"ok": False, "error": "invalid", "message": str(e)}
        row = self.registry.get(name) or {}
        expires_at = ""
        for entry in row.get("aliases", []) or []:
            if entry.get("name") == alias:
                expires_at = entry.get("expires_at", "")
                break
        return {"ok": True, "expires_at": expires_at}

    def remove_alias(self, name: str, alias: str) -> None:
        """Drop ``alias`` from ``name`` immediately, skipping the grace
        reaper. Idempotent: missing alias / missing project is a no-op,
        matching the registry's ``expire_alias`` semantics.
        """
        self.registry.expire_alias(name, alias)

    # ── ticks ────────────────────────────────────────────────────

    async def reaper_tick(self) -> None:
        """Run one pass of the idle-backend reaper."""
        await _reaper_tick()

    async def alias_expiry_tick(self) -> None:
        """Run one pass of the alias-grace reaper.

        Uses the orchestrator's bound registry so a test can inject a
        tmp-file registry and assert against its post-tick state.
        """
        await _alias_reaper_tick(self.registry)

    async def reconcile_on_startup(self) -> None:
        """Adopt already-running backend units after a router restart.

        Module-level ``reconcile_on_startup`` is the function the
        aiohttp ``on_startup`` hook fires; this method exposes the
        same logic on the orchestrator so direct callers (tests, a
        future CLI ``router reconcile`` subcommand) don't have to
        reach into the module.
        """
        await reconcile_on_startup(None)
