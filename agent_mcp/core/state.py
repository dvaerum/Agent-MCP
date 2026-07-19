# Agent-MCP/agent_mcp/core/state.py
"""Typed registry for process-wide mutable state.

This module is the canonical home for what used to live in
``agent_mcp/core/globals.py``. The previous module is preserved as a
thin compatibility shim that proxies every attribute access (read AND
write) here, so existing consumers (`from agent_mcp.core import globals
as g; g.tasks[...] = ...`) keep working with zero edits.

Why split this out:

* Wave 2 follow-up work (EventBus, repositories) needs a stable
  typed surface to import from without competing for edits to
  ``globals.py``.
* ``mypy --strict`` on this single module enforces that every entry
  carries an explicit type annotation. The shim deliberately
  preserves dynamic-attribute semantics for runtime-injected
  fields (e.g. ``server_start_time``) so we don't have to chase down
  every dynamic write in this groundwork PR.

Behavior parity is the only goal of PR-W2a. Callsite migrations
(``g.tasks`` → ``state.tasks``) land in the follow-up PRs.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import anyio  # For rag_index_task_scope type hint


# --- Core Server State ---------------------------------------------------
# Client ID -> Connection data. Original usage of `connections` may be
# simplified or moved if it's purely SSE-connection tracking by the
# transport layer.
connections: Dict[str, Any] = {}

# Agent Token -> Agent data.
active_agents: Dict[str, Dict[str, Any]] = {}

# Per-project HMAC key for verifying the signed forwarding header the
# router (Wave 2) attaches to operator-cookie requests before proxying
# them to the per-project backend. Format + sign/verify logic live in
# ``agent_mcp.app.forwarding_header``.
#
# Lifecycle (post-Wave-3):
#   * Launcher writes 32 random bytes to a per-project file at backend
#     spawn (``/run/agent-mcp/<proj>/forwarding_hmac``, mode 0600).
#   * Backend's ``--forwarding-hmac-in <path>`` flag reads that file at
#     startup and assigns the bytes to ``g.forwarding_hmac_key``.
#   * Router holds the same key (it wrote it) and uses it to sign the
#     ``X-Agent-MCP-Forwarded-Operator`` header on every proxied
#     dashboard / cookie request.
#
# Wave 1 transitional state: the flag is shipped (this PR) but the
# launcher write side is not (Wave 3). When ``g.forwarding_hmac_key``
# is ``None``, the middleware silently skips the forwarding-header
# check and the agents-table bearer path is the only working auth.
# This keeps spawned agents working while the router catches up.
forwarding_hmac_key: Optional[bytes] = None

# DEPRECATED / diagnostic-only — do NOT use as request identity.
#
# This was the forwarding-header operator id, stamped by
# ``AuthHeaderMiddleware`` before ``await call_next`` and read by
# ``require_operator_session`` after it to build the audit ``user_id``.
# SEC round-4 (AC-race) retired that use: a process-wide global written
# before an await and read after it lets two concurrent forwarding
# requests (two dashboard operators on the same per-project backend)
# interleave, so one operator's action gets audit-logged under the
# other's id. Request identity now flows exclusively through the
# per-request ``Principal`` (``request.state.principal`` +
# ``tools.registry.request_principal`` ContextVar), which is
# copy-per-task and race-safe.
#
# The name is retained (unwritten, unread by the request path) only so
# the ``agent_mcp.core.globals`` compatibility shim's explicit re-export
# list keeps resolving. Slated for removal alongside that shim.
current_operator: Optional[str] = None

# Task ID -> Task data (in-memory cache of tasks).
tasks: Dict[str, Dict[str, Any]] = {}


# --- File and Directory State -------------------------------------------
# filepath -> {"agent_id": ..., "timestamp": ..., "status": ...}.
file_map: Dict[str, Dict[str, Any]] = {}

# agent_id -> absolute_working_directory_path.
agent_working_dirs: Dict[str, str] = {}


# --- Auditing and Agent Management --------------------------------------
# In-memory audit log for the current session. Persistent log is
# `agent_audit.log`.
audit_log: List[Dict[str, Any]] = []

# For cycling Cursor profile numbers.
agent_profile_counter: int = 20

# For cycling through AGENT_COLORS from config.py.
agent_color_index: int = 0


# --- Server Lifecycle ---------------------------------------------------
# Flag to control main server loop and background tasks; handled by
# signal_utils.py.
server_running: bool = True

# Set at startup by app.server_lifecycle.application_startup. Read by
# tools/admin_tools.get_server_status_tool_impl via hasattr-guard. Kept
# Optional rather than required so the hasattr-style guard in admin_tools
# stays meaningful.
server_start_time: Optional[str] = None


# --- Database/VSS State -------------------------------------------------
# Flag to check if sqlite-vec extension loadability has been tested.
global_vss_load_tested: bool = False

# Flag indicating if sqlite-vec extension was successfully loaded during
# the initial test.
global_vss_load_successful: bool = False


# --- Background Task Handles --------------------------------------------
# Handle for the RAG indexing background task, typically managed by an
# anyio.TaskGroup.
rag_index_task_scope: Optional[anyio.abc.CancelScope] = None

# Handle for the Claude Code session monitoring background task.
claude_session_task_scope: Optional[anyio.abc.CancelScope] = None

# Handle for the agent_messages retention pruner background task.
# Configured per-project via project_context["config_message_retention_days"].
# Absent or 0 => no pruning. See features.message_retention.
message_retention_task_scope: Optional[anyio.abc.CancelScope] = None

# Handle for the null-subject backfill sweep background task (Phase 2).
# Only started when AGENT_MCP_SUBJECT_MODEL is set. Titles the NULL-subject
# root-message backlog in batches. See features.subject_backfill.
subject_backfill_task_scope: Optional[anyio.abc.CancelScope] = None

# Handle for the mcp_sessions registry pruner background task. Sweeps
# rows whose `last_seen_at` is older than the configured threshold so a
# crashed / disconnected GET /mcp stream's row gets reaped before
# emitters keep fanning out to it. See features.session_registry_pruner.
session_registry_pruner_task_scope: Optional[anyio.abc.CancelScope] = None


# --- Lifespan startup-completion sentinel -------------------------------
# Set at the END of `app.server_lifecycle.application_startup`. Every
# background task that touches the DB via the SQLAlchemy engine cache
# (`db.engine.get_engine` / `get_session`) MUST `await
# startup_complete_event.wait()` before its first cycle.
#
# Why: `get_engine()` resolves `MCP_PROJECT_DIR` via `get_db_path()`,
# but the env var is only set INSIDE `application_startup` (line 157
# of server_lifecycle.py). The CLI's SSE-mode runner
# (cli.py::run_sse_server_with_bg_tasks) launches background tasks via
# `start_background_tasks(tg)` BEFORE awaiting `server.serve()`, which
# is the call that triggers Starlette's lifespan → `application_startup`.
# Without this gate, the first pruner cycle fires against the wrong DB
# URL (cwd-relative fallback from `get_project_dir()`) and caches that
# engine in `db.engine._engines`. All subsequent queries through the
# ORM then see "no such table: ..." against an empty bystander file.
#
# Tests that don't go through the full lifespan (e.g. unit tests for
# the pruner with a hand-rolled DB) can pre-set this event to skip the
# wait. The default-unset state is safe: tests that run via
# `tests/harness.py::mcp_session` exercise the real lifespan and set
# the event naturally.
startup_complete_event: asyncio.Event = asyncio.Event()


def reset_startup_complete_event() -> None:
    """Replace the startup-complete sentinel with a fresh, cleared Event.

    Test isolation: each test that runs `application_startup` sets the
    event; without a reset, the next test's bg-task-wait check would
    short-circuit and we'd never catch a regression where lifespan
    forgets to signal. `conftest.py::reset_globals` calls this before
    every test.
    """
    global startup_complete_event
    startup_complete_event = asyncio.Event()


# --- wait_for_events long-poll signals (plan Phase 2) -------------------
# Per-agent asyncio.Event signals used by the `wait_for_events` tool to
# block until the agent has new activity (direct messages, broadcasts,
# task assignments / changes). The tool clears its agent's signal,
# waits, and then re-queries the source tables when the signal fires.
#
# Writers `.set()` the signal AFTER their DB commit so any pending
# waiter wakes and re-queries with consistent state:
#
#   * `send_agent_message_tool_impl`     -> recipient
#   * `broadcast_admin_message_tool_impl` -> each per-recipient row
#   * `assign_task_tool_impl` (all modes) -> newly-assigned agent
#   * `update_task_status_tool_impl`     -> currently-assigned agent
#
# Use `signal_for(agent_id)` to lazily create / fetch the Event.
# In-process by design: the project backend is single-process, so we
# don't need Redis pubsub or Postgres LISTEN.
agent_event_signals: Dict[str, asyncio.Event] = {}

# --- Per-agent serialization for wait_for_events (PR-2, retired PR-B) ---
# Reversed by PR-B / v5.0.24 in favor of fan-out. The dict is kept as
# an empty registry so the test fixtures + any external consumers still
# importing the name don't break, but the lock is no longer acquired
# anywhere in the codebase. New code should use
# ``agent_event_waiters`` to ask "is anyone waiting for this agent?".
# Slated for removal in a future grace-period cleanup.
agent_event_locks: Dict[str, asyncio.Lock] = {}

# --- Out-of-band event queue (PR-2, retired PR-B) -----------------------
# Replaced by ``agent_event_waiters`` — each ``wait_for_events`` call
# now owns its own ``asyncio.Queue`` of synthetic events, so multiple
# concurrent waiters each get their own copy of every notification.
# This module-level dict is preserved as an empty shim so the test
# fixtures (which `.clear()` it on teardown) keep working without
# branching on the implementation version.
agent_event_queues: Dict[str, List[Dict[str, Any]]] = {}

# --- Per-call waiter registry (PR-B / v5.0.24) --------------------------
# Each ``wait_for_events`` invocation creates its own ``asyncio.Queue``
# of synthetic events (``unassigned_task_appeared`` and friends — events
# without their own DB row) and registers it under the agent_id. On a
# notify the EventBus walks every queue in the list and ``put_nowait``s
# the event, so two concurrent waiters (e.g. a worker's MCP session +
# a shell-based monitor) each receive every event. The waiter
# deregisters its queue on exit (timeout OR event drained).
#
# DB-backed events (messages, task changes) don't need the fan-out
# queue because every waiter independently re-queries SQLite on wake —
# the SELECT is naturally idempotent across N readers.
#
# The shared ``signal_for(agent_id)`` Event remains the wake edge;
# ``Event.set()`` already releases every coroutine blocked on
# ``Event.wait()``, so fan-out comes for free on the wake side. This
# registry only carries the synthetic events that need to ride
# alongside the wake.
agent_event_waiters: Dict[str, List[asyncio.Queue]] = {}


def signal_for(agent_id: str) -> asyncio.Event:
    """Lazily fetch (or create) the asyncio.Event for `agent_id`.

    Returning an Event means callers can `.set()` (writer side) or
    `.wait()` (waiter side) without further state coordination.

    Note: the dict is shared across the process; one Event per agent
    is fine because we only ever drop edges when the signal flips
    cleared->set, and waiters re-query the DB after waking (so the
    actual data is the source of truth, not the edge count).
    """
    sig = agent_event_signals.get(agent_id)
    if sig is None:
        sig = asyncio.Event()
        agent_event_signals[agent_id] = sig
    return sig


def notify_agent_inbox(agent_id: str) -> None:
    """Wake waiters AND fan out resources/updated to subscribed sessions.

    Since PR-W2b (v5.0.17) this is a one-line shim over the EventBus.
    The bus walks its adapter registry — by default LongPollSignal
    (wakes ``wait_for_events`` via ``signal_for``), StreamingQueue
    (pushes ``notifications/resources/updated`` to GET /mcp sessions
    via ``session_registry.fanout_to_agent``), and AuditLog (env-gated
    DEBUG log). New sinks plug in by calling ``event_bus.register``
    without touching writers.

    Writers (`send_agent_message_tool_impl`, broadcast,
    `assign_task_*`, `update_task_status`) call this exactly once,
    AFTER commit, so the waiter / subscriber's re-query sees the new
    row.

    Signature kept unchanged for backwards compatibility — every
    existing call site (5 sites across ``agent_mcp/tools/``) continues
    to work without edits. Callsite migrations to
    ``event_bus.notify(...)`` directly land in Wave-2c or a follow-up.

    The bus catches per-adapter exceptions internally, so this
    function inherits the "notification side-effects can never crash
    the writer" contract without an extra try/except here.
    """
    # Lazy import to avoid a circular dependency at module load
    # (event_bus's default StreamingQueueAdapter imports
    # session_registry, which transitively imports state).
    from . import event_bus

    event_bus.notify(agent_id, "agent_inbox", None)


def lock_for(agent_id: str) -> asyncio.Lock:
    """Deprecated (PR-B): no-op lock retained for backwards source
    compatibility with any external consumers still importing the
    symbol. The fan-out refactor removed every internal acquire/release;
    this function lazily mints a fresh ``asyncio.Lock`` so callers that
    insist on holding it still get a valid object, but its state has no
    semantic meaning inside this codebase.

    Slated for removal in a future cleanup once we've audited that no
    plugins / forks depend on the export.
    """
    lock = agent_event_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        agent_event_locks[agent_id] = lock
    return lock


def register_waiter(agent_id: str) -> asyncio.Queue:
    """Allocate + register a fresh ``asyncio.Queue`` for this waiter.

    Each ``wait_for_events`` invocation calls this on entry to get its
    own synthetic-event queue. The notifier (``LongPollSignalAdapter``)
    walks the list under ``agent_id`` and ``put_nowait``s each event
    onto every queue, so two concurrent waiters each see every event.

    The waiter MUST call :func:`unregister_waiter` on exit (timeout OR
    event drained) to avoid leaking queues.
    """
    queue: asyncio.Queue = asyncio.Queue()
    waiters = agent_event_waiters.get(agent_id)
    if waiters is None:
        waiters = []
        agent_event_waiters[agent_id] = waiters
    waiters.append(queue)
    return queue


def unregister_waiter(agent_id: str, queue: asyncio.Queue) -> None:
    """Remove ``queue`` from this agent's waiter list. Idempotent — a
    double-unregister (e.g. from a finally block that overlaps a
    cleanup path) is a no-op."""
    waiters = agent_event_waiters.get(agent_id)
    if not waiters:
        return
    try:
        waiters.remove(queue)
    except ValueError:
        pass
    if not waiters:
        # Tidy: drop the empty list so iterating agents stays cheap.
        agent_event_waiters.pop(agent_id, None)


def waiter_count(agent_id: str) -> int:
    """How many ``wait_for_events`` calls are currently parked for this
    agent. Used by the dashboard's ``/api/all-data`` to compute
    ``wait_for_events_in_flight`` (==`>0`)."""
    waiters = agent_event_waiters.get(agent_id)
    return len(waiters) if waiters else 0


# Sentinel object pushed onto a waiter queue purely as a wake edge
# (no payload — the waiter is expected to re-query the DB for new
# rows). ``None`` is used so a simple ``if item is None: continue``
# in the drain loop filters it out cheaply.
WAITER_WAKE_SENTINEL: Optional[Dict[str, Any]] = None


def notify_waiters(agent_id: str) -> None:
    """Wake every registered ``wait_for_events`` caller for this agent
    by putting a sentinel on their private queues. Used by writers of
    DB-backed events (messages, task changes) so the waiter exits its
    ``queue.get()`` slice and re-queries SQLite for the new row.

    For synthetic event types (e.g. ``unassigned_task_appeared``) use
    :func:`dispatch_synthetic_event` instead — that pushes the actual
    event onto every queue, which also serves as the wake.
    """
    waiters = agent_event_waiters.get(agent_id)
    if not waiters:
        return
    for queue in list(waiters):
        try:
            queue.put_nowait(WAITER_WAKE_SENTINEL)
        except Exception:  # pragma: no cover - defensive
            pass


def dispatch_synthetic_event(
    agent_id: str, event: Dict[str, Any],
) -> None:
    """Fan-out ``event`` onto every registered waiter queue for
    ``agent_id``. Used by ``LongPollSignalAdapter`` for events that
    don't have their own DB row (``unassigned_task_appeared``).

    If no waiters are registered the event is dropped on the floor —
    that's the intended semantic. The synthetic events are wake-edge
    notifications, not durable work-tickets; an agent without an
    in-flight wait will pick up the underlying task via its next
    ``fetch_events_since`` / ``view_tasks(status=unassigned)`` catch-up
    instead.
    """
    waiters = agent_event_waiters.get(agent_id)
    if not waiters:
        return
    # Snapshot the list — a waiter that wakes during this iteration
    # may unregister itself; mutating mid-iteration would skip a peer.
    for queue in list(waiters):
        try:
            queue.put_nowait(event)
        except Exception:  # pragma: no cover - defensive
            # An unbounded asyncio.Queue.put_nowait should not raise;
            # if a future shape does, drop this waiter's event rather
            # than poisoning the rest of the fanout.
            pass


def push_event(agent_id: str, event: Dict[str, Any]) -> None:
    """Deprecated (PR-B). Forward synthetic events through
    :func:`dispatch_synthetic_event` so existing callers (legacy
    EventBus adapter shape) still reach the new waiter registry.
    """
    dispatch_synthetic_event(agent_id, event)


def drain_events(agent_id: str) -> List[Dict[str, Any]]:
    """Deprecated (PR-B). The pre-fan-out impl drained a shared queue;
    with per-waiter queues this function has nothing to return —
    waiters drain their own queue directly via
    :func:`drain_waiter_queue`. Retained as an empty-list shim so any
    external consumer (or older test snapshot) that still calls it
    sees the same shape it used to."""
    return []


def drain_waiter_queue(queue: asyncio.Queue) -> List[Dict[str, Any]]:
    """Pop everything currently in ``queue`` without blocking and
    return as a list. Called by ``wait_for_events_tool_impl`` on every
    wake to harvest synthetic events that were fanned out while the
    waiter was parked.

    Wake-only sentinels (``WAITER_WAKE_SENTINEL``, currently ``None``)
    are filtered out — they exist purely to release the waiter's
    ``queue.get()`` call so it can re-query the DB.
    """
    out: List[Dict[str, Any]] = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is WAITER_WAKE_SENTINEL:
            continue
        out.append(item)
    return out


def notify_unassigned_task_appeared(task_id: str) -> None:
    """Fan out an ``unassigned_task_appeared`` event to every active agent.

    PR5 retired the structured capability-tag routing: the old
    ``req ⊆ caps`` subset-match was already a no-op (an empty required
    set matched everyone, and no project ever populated the tags), so
    every unassigned task now surfaces to every active, non-admin agent
    unconditionally. The retired capability-tag columns (on the agents
    and tasks tables) are gone.

    Each agent gets a skinny event pushed to its queue + its signal set
    so any in-flight ``wait_for_events`` wakes immediately.

    Since PR-W2b (v5.0.17) the per-agent wake is routed through the
    EventBus: this function calls ``event_bus.notify(agent_id,
    "unassigned_task_appeared", payload)`` for each active agent. The
    default ``LongPollSignalAdapter`` handles the legacy ``push_event`` +
    signal set so ``wait_for_events`` keeps draining synthetic events;
    the ``StreamingQueueAdapter`` additionally publishes the event to GET
    /mcp subscribers.

    Wrapped in broad try/except so notification side-effects can never
    poison the source write (the unassigned task is already persisted
    by the time we're called).
    """
    try:
        # Lazy import to avoid circular dependency.
        from ..db.connection import get_db_connection
        from . import event_bus
        import datetime as _dt

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT task_id, title, priority, created_at "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            task_row = cursor.fetchone()
            if task_row is None:
                # Source row gone - nothing to fan out. (Race with
                # delete_task; not our problem.)
                return

            # "Active" excludes BOTH 'terminated' AND 'tombstone'
            # (purge-cascade FK artefacts) — a tombstone row must
            # never receive an unassigned-task fan-out (BL-R31-3b).
            # arch-deepening F: function-level import avoids the
            # state<->agent_repository module-load cycle.
            from ..repositories.agent_repository import LIVE_AGENT_SQL

            cursor.execute(
                f"SELECT agent_id FROM agents WHERE {LIVE_AGENT_SQL}",
            )
            agent_rows = cursor.fetchall()
        finally:
            conn.close()

        # ``ref_id`` and ``timestamp`` ride on the payload so the
        # LongPollSignalAdapter can reconstruct the legacy
        # ``{"type", "ref_id", "timestamp", "payload"}`` event shape
        # that ``wait_for_events`` already drains from
        # ``agent_event_queues``. The bus contract (agent_id,
        # event_type, payload) intentionally stays flat — adapters
        # pull the meta out of ``payload`` when they need it.
        timestamp = _dt.datetime.now().isoformat()
        payload = {
            "ref_id": task_row["task_id"],
            "timestamp": timestamp,
            "task_id": task_id,
            "title": task_row["title"],
            "priority": task_row["priority"],
        }

        for row in agent_rows:
            agent_id = row["agent_id"]
            # Admin pseudo-agent never wakes for unassigned tasks (PR
            # #117 made admin a real DB row but admins don't run worker
            # loops).
            if agent_id and agent_id.lower().startswith("admin"):
                continue
            event_bus.notify(agent_id, "unassigned_task_appeared", payload)
    except Exception:  # pragma: no cover - defensive
        # Source write is committed; notification is best-effort.
        pass


def wake_all_for_flag_recheck() -> None:
    """Wake every agent with a pending signal so they re-evaluate the
    auto_event_loop flags.

    Called by the toggle-write paths (per-agent and global) so any
    in-flight ``wait_for_events`` returns within ~immediately and the
    impl can return a ``stop_listening`` envelope when the new flag
    state requires it.
    """
    for agent_id in list(agent_event_signals.keys()):
        try:
            agent_event_signals[agent_id].set()
        except Exception:  # pragma: no cover - defensive
            pass
    # PR-B fan-out: also poke every registered waiter queue so the new
    # queue-based slow path wakes alongside the legacy shared-event one.
    for agent_id in list(agent_event_waiters.keys()):
        try:
            notify_waiters(agent_id)
        except Exception:  # pragma: no cover - defensive
            pass


def wake_for_flag_recheck(agent_id: str) -> None:
    """Wake every in-flight ``wait_for_events`` call for ``agent_id``
    so each one re-evaluates the ``auto_event_loop`` flags. Used by
    the dashboard toggle handler so an operator who flips the flag
    sees waiters return ``stop_listening`` within a slice tick.

    Pre-fan-out this set the shared ``signal_for(agent_id)`` event;
    PR-B / v5.0.24 routes it through the per-waiter queues so the new
    queue-based slow path actually wakes (plus the legacy event for
    any third-party that still ``await``s it).
    """
    try:
        signal_for(agent_id).set()
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        notify_waiters(agent_id)
    except Exception:  # pragma: no cover - defensive
        pass


