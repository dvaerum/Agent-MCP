# Agent-MCP/mcp_template/mcp_server_src/core/globals.py
"""
Centralized mutable global state for the MCP server.
To use:
from mcp_server_src.core import globals as g
g.admin_token = "new_token"
"""
import asyncio
import anyio  # For rag_index_task type hint
from typing import Dict, List, Optional, Any

# --- Core Server State ---
# From main.py:147
# Client ID -> Connection data (Note: original usage of 'connections' might be simplified
# or its management moved if it's purely for SSE connection tracking by the transport layer)
connections: Dict[str, Any] = {}

# From main.py:148
active_agents: Dict[str, Dict[str, Any]] = {}  # Agent Token -> Agent data

# From main.py:149
# This is the runtime admin_token.
# Initialization logic (generate/load) will be handled during server startup.
admin_token: Optional[str] = None

# From main.py:150
tasks: Dict[str, Dict[str, Any]] = {}  # Task ID -> Task data (in-memory cache of tasks)

# --- File and Directory State ---
# From main.py:153
file_map: Dict[str, Dict[str, Any]] = (
    {}
)  # filepath -> {"agent_id": ..., "timestamp": ..., "status": ...}

# From main.py:154
agent_working_dirs: Dict[str, str] = {}  # agent_id -> absolute_working_directory_path

# --- Tmux Session Management ---
# Maps agent_id -> tmux session name for tracking active agent sessions
agent_tmux_sessions: Dict[str, str] = {}  # agent_id -> tmux_session_name

# --- Auditing and Agent Management ---
# From main.py:155
# In-memory audit log for the current session. Persistent log is 'agent_audit.log'.
audit_log: List[Dict[str, Any]] = []

# From main.py:158
agent_profile_counter: int = 20  # For cycling Cursor profile numbers

# From main.py:166
agent_color_index: int = 0  # For cycling through AGENT_COLORS from config.py

# --- Server Lifecycle ---
# From main.py:169
server_running: bool = (
    True  # Flag to control main server loop and background tasks, handled by signal_utils.py
)

# --- External Service Clients (Placeholders) ---
# From main.py:185
# The actual OpenAI client instance will be initialized and managed by external/openai_service.py.
# This global variable can serve as a reference if truly global access is needed,
# though passing the client explicitly or using a getter from openai_service is cleaner.
# For now, we'll keep it as a placeholder reflecting the original structure.
# Type hint can be refined to `openai.OpenAI` once that module is structured.
openai_client_instance: Optional[Any] = None

# --- Database/VSS State ---
# From main.py:200
# Flag to check if sqlite-vec extension loadability has been tested.
global_vss_load_tested: bool = False

# From main.py:201
# Flag indicating if sqlite-vec extension was successfully loaded during the initial test.
global_vss_load_successful: bool = False

# --- Background Task Handles ---
# From main.py:510 (and used in main.py:1943, 2627, 2641)
# Handle for the RAG indexing background task, typically managed by an anyio.TaskGroup.
# The type hint `anyio.abc.CancelScope` is a common way to hold a reference that allows cancellation.
rag_index_task_scope: Optional[anyio.abc.CancelScope] = None

# Handle for the Claude Code session monitoring background task
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

# --- Lifespan startup-completion sentinel ---
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
# ORM then see "no such table: …" against an empty bystander file.
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

# --- wait_for_events long-poll signals (plan Phase 2) ---
# Per-agent asyncio.Event signals used by the `wait_for_events` tool to
# block until the agent has new activity (direct messages, broadcasts,
# task assignments / changes). The tool clears its agent's signal,
# waits, and then re-queries the source tables when the signal fires.
#
# Writers `.set()` the signal AFTER their DB commit so any pending
# waiter wakes and re-queries with consistent state:
#
#   * `send_agent_message_tool_impl`     → recipient
#   * `broadcast_admin_message_tool_impl` → each per-recipient row
#   * `assign_task_tool_impl` (all modes) → newly-assigned agent
#   * `update_task_status_tool_impl`     → currently-assigned agent
#
# Use `signal_for(agent_id)` to lazily create / fetch the Event.
# In-process by design: the project backend is single-process, so we
# don't need Redis pubsub or Postgres LISTEN.
agent_event_signals: Dict[str, asyncio.Event] = {}


def signal_for(agent_id: str) -> asyncio.Event:
    """Lazily fetch (or create) the asyncio.Event for `agent_id`.

    Returning an Event means callers can `.set()` (writer side) or
    `.wait()` (waiter side) without further state coordination.

    Note: the dict is shared across the process; one Event per agent
    is fine because we only ever drop edges when the signal flips
    cleared→set, and waiters re-query the DB after waking (so the
    actual data is the source of truth, not the edge count).
    """
    sig = agent_event_signals.get(agent_id)
    if sig is None:
        sig = asyncio.Event()
        agent_event_signals[agent_id] = sig
    return sig


def notify_agent_inbox(agent_id: str) -> None:
    """Wake waiters AND fan out resources/updated to subscribed sessions.

    Single call site for the "new event for this agent" trigger. Two
    sinks:

    1. ``signal_for(agent_id).set()`` — wakes any in-process
       ``wait_for_events`` tool call (POST /mcp blocking on the Event).
       This is the long-poll path workers rely on today.

    2. ``session_registry.fanout_to_agent`` — enqueues
       ``notifications/resources/updated`` on every GET /mcp stream
       registered for the agent. The stream-side draining loop ships
       these to the wire (Phase: transport-wiring); until that lands
       the queue accumulates and the in-process signal still works,
       so worker UX is unchanged.

    Writers (`send_agent_message_tool_impl`, broadcast,
    `assign_task_*`, `update_task_status`) call this exactly once,
    AFTER commit, so the waiter / subscriber's re-query sees the new
    row.

    Wrapped in broad try/except because notification side-effects must
    never crash the tool that called the writer — the source-of-truth
    write is already committed.
    """
    try:
        signal_for(agent_id).set()
    except Exception:  # pragma: no cover - defensive
        pass

    try:
        # Lazy import to avoid a circular dependency at module load
        # (session_registry → db.connection → core.config).
        from . import session_registry

        payload = {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": f"agent-mcp://inbox/{agent_id}"},
        }
        session_registry.fanout_to_agent(agent_id, payload)
    except Exception:  # pragma: no cover - defensive
        pass

# Note: The original `main.py` also had `openai_client = None` at line 185.
# I've named it `openai_client_instance` here to avoid confusion with the module name
# if we later have `import openai_client from ...`.
# The actual OpenAI client will be initialized and managed in `external/openai_service.py`.
