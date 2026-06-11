# Agent-MCP/agent_mcp/runtime/__init__.py
"""Agent runtime package — the named home of the "boot, prompt, discover,
tear-down an agent" concept.

Round 1 (PRs #146–#155) promoted module-of-functions repositories to
the class-based ``TaskRepository`` / ``AgentRepository`` /
``MessageRepository``. PR #156 (round 2 PR A) did the same for the
atomic-write seam.  This package (round 2 PR B) does the same for
what was hiding inside ``utils/tmux_utils.py`` (564 lines) and
``utils/worktree_utils.py`` (576 lines): the agent runtime.

Import pattern at call sites::

    from agent_mcp.runtime import agent_runtime
    rt = agent_runtime.get_runtime()
    rt.send_prompt(session_name, prompt)
    rt.cleanup(session_name)

Or for ad-hoc use of the free-function primitives (e.g.
``sanitize_session_name``, ``generate_agent_session_name``)::

    from agent_mcp.runtime.agent_runtime import sanitize_session_name

The legacy ``agent_mcp.utils.tmux_utils`` and
``agent_mcp.utils.worktree_utils`` modules remain as ~40-line
re-export shims so existing call sites keep working unchanged (same
canonical shim shape as ``agent_mcp.db.actions.task_db`` post-PR-#153).
"""
from __future__ import annotations

from . import agent_runtime
from .agent_runtime import AgentRuntime, get_runtime

__all__ = [
    "AgentRuntime",
    "agent_runtime",
    "get_runtime",
]
