# Agent-MCP/agent_mcp/utils/worktree_utils.py
"""Backwards-compatible re-export shim for the git-worktree side of the
agent runtime.

.. deprecated:: PR B of round 2 (architecture-review series — the
   "AgentRuntime promotion")
   The implementations of these git-worktree primitives moved to
   :mod:`agent_mcp.runtime.agent_runtime` so the :class:`AgentRuntime`
   class is the named home of the "boot, prompt, discover, tear-down
   an agent" concept. The companion file
   :mod:`agent_mcp.utils.tmux_utils` moved at the same time.

   This module remains as a thin re-export so existing importers keep
   working unchanged:

   * ``agent_mcp.features.worktree_integration``

   New code should import from :mod:`agent_mcp.runtime.agent_runtime`
   (or hold a reference to :class:`AgentRuntime` and call
   ``rt.create_worktree(...)``).

Same canonical shim shape as :mod:`agent_mcp.db.actions.task_db`
post-PR-#153 — function signatures + return shapes are preserved 1:1
because the re-exports are the *same callables*, not wrappers.
"""

from __future__ import annotations

from ..runtime.agent_runtime import (
    branch_exists,
    cleanup_git_worktree,
    create_git_worktree,
    detect_project_setup_commands,
    generate_branch_name,
    generate_worktree_path,
    get_current_branch,
    has_uncommitted_changes,
    is_git_repository,
    list_git_worktrees,
    run_setup_commands,
    validate_worktree_requirements,
)

__all__ = [
    "branch_exists",
    "cleanup_git_worktree",
    "create_git_worktree",
    "detect_project_setup_commands",
    "generate_branch_name",
    "generate_worktree_path",
    "get_current_branch",
    "has_uncommitted_changes",
    "is_git_repository",
    "list_git_worktrees",
    "run_setup_commands",
    "validate_worktree_requirements",
]
