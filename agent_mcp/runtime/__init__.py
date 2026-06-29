# Agent-MCP/agent_mcp/runtime/__init__.py
"""Agent runtime package — homes the git-worktree primitives that
support :mod:`agent_mcp.features.worktree_integration`.

Wave 7 PR 3 (coordinator transition, 2026-06-29) deleted the
spawn-claude-via-tmux module that used to live alongside the worktree
helpers. agent-mcp no longer owns user-side claude processes — the
user owns them; agent-mcp mints tokens via
``register_agent_tool_impl`` (operator-only, dashboard surface).

Surviving public surface::

    from agent_mcp.runtime.worktree import cleanup_git_worktree
"""
from __future__ import annotations

from . import worktree

__all__ = [
    "worktree",
]
