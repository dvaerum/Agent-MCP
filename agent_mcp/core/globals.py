# Agent-MCP/agent_mcp/core/globals.py
"""Backwards-compatibility shim for ``agent_mcp.core.globals``.

The canonical home for process-wide mutable state is now
``agent_mcp.core.state``. This module exists so every existing
``from agent_mcp.core import globals as g`` import keeps resolving to
the same state - including writes (``g.server_running = False``).

How the alias works
-------------------
At import time we replace this module in ``sys.modules`` with the
``state`` module instance. After the swap, every attribute access -
read OR write - that goes through ``agent_mcp.core.globals`` lands on
``agent_mcp.core.state``. The two import paths share one module object,
so there is exactly one source of truth.

This is the standard PEP 562-era pattern for module aliasing without
fooling type-checkers. Static analysis tools that follow imports will
see the names defined in ``state.py`` (we re-export the entire
public surface below for that benefit) and treat both module paths as
equivalent.

PR-W2a (v5.0.16) introduced this shim. Wave 2 follow-up PRs migrate
callsites to import from ``agent_mcp.core.state`` directly; this shim
stays for at least one release after that migration to give downstream
consumers (Nix module, dashboard, third-party tools) a deprecation
window.
"""
from __future__ import annotations

import sys

from agent_mcp.core import state as _state

# Re-export the full public surface so static analyzers and IDEs can
# resolve ``agent_mcp.core.globals.tasks`` etc. without following the
# sys.modules swap. (At runtime the swap below makes this redundant
# because the module *is* ``_state``, but tools that parse Python
# statically benefit from explicit names.)
from agent_mcp.core.state import (  # noqa: F401
    active_agents,
    agent_color_index,
    agent_event_locks,
    agent_event_queues,
    agent_event_signals,
    agent_event_waiters,
    agent_profile_counter,
    agent_working_dirs,
    audit_log,
    claude_session_task_scope,
    connections,
    current_operator,
    dispatch_synthetic_event,
    drain_events,
    drain_waiter_queue,
    file_map,
    forwarding_hmac_key,
    global_vss_load_successful,
    global_vss_load_tested,
    lock_for,
    message_retention_task_scope,
    notify_agent_inbox,
    notify_unassigned_task_appeared,
    notify_waiters,
    push_event,
    rag_index_task_scope,
    WAITER_WAKE_SENTINEL,
    register_waiter,
    reset_startup_complete_event,
    server_running,
    server_start_time,
    session_registry_pruner_task_scope,
    signal_for,
    startup_complete_event,
    tasks,
    unregister_waiter,
    waiter_count,
    wake_all_for_flag_recheck,
    wake_for_flag_recheck,
)

# Explicit re-export list so ``from agent_mcp.core import globals as g``
# followed by ``g.global_vss_load_successful`` passes mypy --strict
# without triggering the "does not explicitly export" attr-defined
# check. The runtime sys.modules swap below makes the list redundant
# for execution, but static analyzers (mypy, pyright) follow this file
# instead of the swap.
__all__ = [
    "active_agents",
    "agent_color_index",
    "agent_event_locks",
    "agent_event_queues",
    "agent_event_signals",
    "agent_event_waiters",
    "agent_profile_counter",
    "agent_working_dirs",
    "audit_log",
    "claude_session_task_scope",
    "connections",
    "current_operator",
    "dispatch_synthetic_event",
    "drain_events",
    "drain_waiter_queue",
    "file_map",
    "forwarding_hmac_key",
    "global_vss_load_successful",
    "global_vss_load_tested",
    "lock_for",
    "message_retention_task_scope",
    "notify_agent_inbox",
    "notify_unassigned_task_appeared",
    "notify_waiters",
    "push_event",
    "rag_index_task_scope",
    "WAITER_WAKE_SENTINEL",
    "register_waiter",
    "reset_startup_complete_event",
    "server_running",
    "server_start_time",
    "session_registry_pruner_task_scope",
    "signal_for",
    "startup_complete_event",
    "tasks",
    "unregister_waiter",
    "waiter_count",
    "wake_all_for_flag_recheck",
    "wake_for_flag_recheck",
]

# Swap this module entry in sys.modules with the state module so that
# every subsequent ``from agent_mcp.core import globals as g`` resolves
# to the same module object as ``agent_mcp.core.state``. There's no
# diverged copy of state.
#
# This must happen AFTER the explicit re-exports above so import-time
# name resolution succeeds (the explicit imports run against this
# module's namespace, not the swapped one).
sys.modules[__name__] = _state
