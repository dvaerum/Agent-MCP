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

# Runtime admin token. Initialization logic (generate/load) lives in
# server startup.
admin_token: Optional[str] = None

# Task ID -> Task data (in-memory cache of tasks).
tasks: Dict[str, Dict[str, Any]] = {}


# --- File and Directory State -------------------------------------------
# filepath -> {"agent_id": ..., "timestamp": ..., "status": ...}.
file_map: Dict[str, Dict[str, Any]] = {}

# agent_id -> absolute_working_directory_path.
agent_working_dirs: Dict[str, str] = {}


# --- Tmux Session Management --------------------------------------------
# agent_id -> tmux_session_name.
agent_tmux_sessions: Dict[str, str] = {}


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


# --- External Service Clients (placeholders) ----------------------------
# The actual OpenAI client instance is initialized and managed by
# external/openai_service.py. This global serves as a reference if truly
# global access is needed.
openai_client_instance: Optional[Any] = None


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

# --- Per-agent serialization for wait_for_events (PR-2) -----------------
# Spec: only one `wait_for_events` call per agent at a time. A second
# concurrent call returns immediately with an
# `{"error": "another_wait_in_flight", ...}` envelope (HTTP-409 analog).
# The lock is acquired non-blocking by the tool impl - if it's already
# held we return the conflict shape without queueing.
agent_event_locks: Dict[str, asyncio.Lock] = {}

# --- Out-of-band event queue (PR-2) -------------------------------------
# `signal_for(agent_id)` + DB re-query covers everything that has its
# own table row (messages, task changes). The
# `unassigned_task_appeared` event has no per-recipient row - it's
# fanned out in-Python to capability-matched agents - so we need a
# transient per-agent queue for synthetic events that don't materialise
# from a SELECT. Drained by `wait_for_events_tool_impl` on wake; never
# persisted (intentional, since the agent can always
# `view_tasks(status=unassigned)` to reconstruct missed events).
agent_event_queues: Dict[str, List[Dict[str, Any]]] = {}


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
    """Lazily fetch (or create) the per-agent serialization lock.

    Used by ``wait_for_events_tool_impl`` to enforce one-call-per-agent.
    `acquire(blocking=False)` returns False if another call is already
    in flight; the tool then returns the conflict envelope without
    queueing.
    """
    lock = agent_event_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        agent_event_locks[agent_id] = lock
    return lock


def push_event(agent_id: str, event: Dict[str, Any]) -> None:
    """Append a synthetic event to `agent_id`'s out-of-band queue.

    Used for events that have no DB row of their own (notably
    ``unassigned_task_appeared``). The queue is drained on the next
    ``wait_for_events`` wake.

    Wrapped in try/except by callers; this function itself is total
    (list.append never raises on bounded inputs).
    """
    queue = agent_event_queues.get(agent_id)
    if queue is None:
        queue = []
        agent_event_queues[agent_id] = queue
    queue.append(event)


def drain_events(agent_id: str) -> List[Dict[str, Any]]:
    """Pop and return all queued synthetic events for `agent_id`.

    Returns an empty list if no events are queued. The list is detached
    from internal state on return - callers may freely mutate it.
    """
    queue = agent_event_queues.get(agent_id)
    if not queue:
        return []
    out = list(queue)
    queue.clear()
    return out


def notify_unassigned_task_appeared(
    task_id: str,
    task_required_capabilities: List[str],
) -> None:
    """Fan out an ``unassigned_task_appeared`` event to every agent
    whose capabilities satisfy the task's `required_capabilities`.

    Subset semantics (locked design decision):
      * Empty ``task_required_capabilities`` -> wake every active agent.
      * Empty ``agent.capabilities`` -> wake only when the task also has
        empty required (no labels to satisfy).
      * Non-empty both -> wake when ``agent.capabilities`` is a superset.

    Implemented in-Python because we don't need SQL-indexed matching at
    sub-100-agent scale. Each match pushes a skinny event to the
    target agent's queue + sets their signal so any in-flight
    ``wait_for_events`` wakes immediately.

    Since PR-W2b (v5.0.17) the per-agent wake is routed through the
    EventBus: this function does the DB capability-matching and then
    calls ``event_bus.notify(agent_id, "unassigned_task_appeared",
    payload)`` for each matched agent. The default
    ``LongPollSignalAdapter`` handles the legacy ``push_event`` + signal
    set so ``wait_for_events`` keeps draining synthetic events; the
    ``StreamingQueueAdapter`` additionally publishes the event to GET
    /mcp subscribers.

    Wrapped in broad try/except so notification side-effects can never
    poison the source write (the unassigned task is already persisted
    by the time we're called).
    """
    try:
        # Lazy import to avoid circular dependency.
        from ..db.connection import get_db_connection
        from ..utils.capability_normalization import normalize_capabilities
        from . import event_bus
        import datetime as _dt
        import json as _json

        # Normalize the task's required caps once. PR-1 already
        # normalizes at write time so this is idempotent; defensive
        # against tests that bypass the normalizer.
        required = normalize_capabilities(task_required_capabilities or [])
        required_set = set(required)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT task_id, title, priority, required_capabilities, "
                "       created_at "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            task_row = cursor.fetchone()
            if task_row is None:
                # Source row gone - nothing to fan out. (Race with
                # delete_task; not our problem.)
                return

            cursor.execute(
                "SELECT agent_id, capabilities FROM agents "
                "WHERE status != 'terminated'",
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
            "required_capabilities": list(required),
        }

        for row in agent_rows:
            agent_id = row["agent_id"]
            # Admin pseudo-agent never wakes for unassigned tasks (PR
            # #117 made admin a real DB row but admins don't run worker
            # loops).
            if agent_id and agent_id.lower().startswith("admin"):
                continue
            try:
                raw_caps = row["capabilities"] or "[]"
                caps_list = (
                    _json.loads(raw_caps) if isinstance(raw_caps, str) else list(raw_caps)
                )
            except Exception:
                caps_list = []
            agent_caps = set(normalize_capabilities(caps_list))

            # Subset match: required is subset of agent_caps.
            # Empty required -> matches everyone (set.issubset(empty) is True).
            if not required_set.issubset(agent_caps):
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


def wake_for_flag_recheck(agent_id: str) -> None:
    """Wake one agent's in-flight wait so it re-evaluates flags."""
    try:
        signal_for(agent_id).set()
    except Exception:  # pragma: no cover - defensive
        pass
