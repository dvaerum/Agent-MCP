# Agent-MCP/agent_mcp/utils/tmux_utils.py
"""Backwards-compatible re-export shim for the tmux side of the agent runtime.

.. deprecated:: PR B of round 2 (architecture-review series — the
   "AgentRuntime promotion")
   The implementations of these tmux primitives moved to
   :mod:`agent_mcp.runtime.agent_runtime` so the :class:`AgentRuntime`
   class is the named home of the "boot, prompt, discover, tear-down
   an agent" concept. The companion file
   :mod:`agent_mcp.utils.worktree_utils` moved at the same time.

   This module remains as a thin re-export so existing importers keep
   working unchanged:

   * ``agent_mcp.tools.admin_tools``
   * ``agent_mcp.tools.task_tools``
   * ``agent_mcp.tools.agent_communication_tools``

   New code should import from :mod:`agent_mcp.runtime.agent_runtime`
   (or hold a reference to :class:`AgentRuntime` for the small
   intention-revealing interface: ``send_prompt`` / ``discover_active``
   / ``is_alive`` / ``cleanup`` / ``create_worktree``).

Same canonical shim shape as :mod:`agent_mcp.db.actions.task_db`
post-PR-#153 — function signatures + return shapes are preserved 1:1
because the re-exports are the *same callables*, not wrappers.
"""

from __future__ import annotations

from ..runtime.agent_runtime import (
    cleanup_agent_sessions,
    create_tmux_session,
    discover_active_agents_from_tmux,
    generate_agent_session_name,
    get_admin_token_suffix,
    get_session_status,
    is_tmux_available,
    kill_tmux_session,
    list_tmux_sessions,
    parse_agent_session_name,
    sanitize_session_name,
    send_command_to_session,
    send_prompt_async,
    send_prompt_to_session,
    session_exists,
    sync_agents_from_tmux,
)

__all__ = [
    "cleanup_agent_sessions",
    "create_tmux_session",
    "discover_active_agents_from_tmux",
    "generate_agent_session_name",
    "get_admin_token_suffix",
    "get_session_status",
    "is_tmux_available",
    "kill_tmux_session",
    "list_tmux_sessions",
    "parse_agent_session_name",
    "sanitize_session_name",
    "send_command_to_session",
    "send_prompt_async",
    "send_prompt_to_session",
    "session_exists",
    "sync_agents_from_tmux",
]
