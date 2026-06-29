"""Negative-assertion guards for Wave 7 PR 3 (coordinator transition).

PR 3 deleted the claude-spawn machinery wholesale: the
``agent_mcp/runtime/agent_runtime.py`` module, the
``agent_mcp/utils/tmux_utils.py`` + ``utils/worktree_utils.py`` shim
files, the ``agent_mcp/templates/agent_startup.sh`` template, the
spawn-using ``create_agent`` + ``relaunch_agent`` tools in
``agent_mcp/tools/admin_tools.py``, the post-completion
auto-launch-testing-agent block in ``agent_mcp/tools/task_tools.py``,
and the tmux delivery branch in
``agent_mcp/tools/agent_communication_tools.py``. agent-mcp no longer
spawns claude processes — the operator registers an agent via
``register_agent_tool_impl`` and the user owns the resulting claude
session.

These tests pin the absence of the deleted surfaces so the next
round of changes can't regress to spawning claude under the hood. See
``Wave 7`` in ``/home/dennis/.claude/plans/prancy-napping-pie.md`` for
the design rationale.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_TOOLS_PY = REPO_ROOT / "agent_mcp" / "tools" / "admin_tools.py"
TASK_TOOLS_PY = REPO_ROOT / "agent_mcp" / "tools" / "task_tools.py"
COMM_TOOLS_PY = REPO_ROOT / "agent_mcp" / "tools" / "agent_communication_tools.py"


# ── Deleted module: agent_mcp.runtime.agent_runtime ──────────────────


def test_agent_runtime_module_is_gone() -> None:
    """The whole spawn-side module is deleted."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_mcp.runtime.agent_runtime")


def test_create_tmux_session_is_not_importable() -> None:
    """The spawn primitive is gone (was at runtime.agent_runtime)."""
    with pytest.raises(ImportError):
        from agent_mcp.runtime.agent_runtime import create_tmux_session  # noqa: F401


def test_kill_tmux_session_is_not_importable() -> None:
    """The teardown primitive is gone."""
    with pytest.raises(ImportError):
        from agent_mcp.runtime.agent_runtime import kill_tmux_session  # noqa: F401


def test_send_prompt_async_is_not_importable() -> None:
    """The prompt-delivery primitive is gone (no tmux pane to push to)."""
    with pytest.raises(ImportError):
        from agent_mcp.runtime.agent_runtime import send_prompt_async  # noqa: F401


# ── Deleted shim modules under agent_mcp.utils ────────────────────────


def test_utils_tmux_utils_module_is_gone() -> None:
    """The legacy ``utils.tmux_utils`` re-export shim is deleted."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_mcp.utils.tmux_utils")


def test_utils_worktree_utils_module_is_gone() -> None:
    """The legacy ``utils.worktree_utils`` shim is deleted; callers
    now import from :mod:`agent_mcp.runtime.worktree` directly."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_mcp.utils.worktree_utils")


# ── Preserved: worktree primitives at the new home ────────────────────


def test_worktree_primitives_survive_at_new_home() -> None:
    """The worktree side of the old ``agent_runtime`` module survives
    at :mod:`agent_mcp.runtime.worktree`. ``cleanup_git_worktree`` is
    the cardinal symbol (the brief calls it out explicitly)."""
    from agent_mcp.runtime.worktree import (
        cleanup_git_worktree,
        create_git_worktree,
        is_git_repository,
    )

    assert callable(cleanup_git_worktree)
    assert callable(create_git_worktree)
    assert callable(is_git_repository)


def test_features_worktree_integration_still_works() -> None:
    """The downstream caller of the worktree primitives still imports
    cleanly. The migration target was :mod:`agent_mcp.runtime.worktree`."""
    mod = importlib.import_module("agent_mcp.features.worktree_integration")
    assert hasattr(mod, "WorktreeManager")
    assert hasattr(mod, "cleanup_agent_worktree")


# ── Deleted spawn-shaped admin tools ──────────────────────────────────


def test_create_agent_tool_impl_is_gone() -> None:
    """The spawn-via-tmux ``create_agent_tool_impl`` is deleted —
    ``register_agent_tool_impl`` is the sole agent-creation tool."""
    admin_tools = importlib.import_module("agent_mcp.tools.admin_tools")
    assert not hasattr(admin_tools, "create_agent_tool_impl")
    assert hasattr(admin_tools, "register_agent_tool_impl")


def test_relaunch_agent_tool_impl_is_gone() -> None:
    """The ``relaunch_agent`` tool (tmux send-keys to an existing
    session) has no analogue under the coordinator model."""
    admin_tools = importlib.import_module("agent_mcp.tools.admin_tools")
    assert not hasattr(admin_tools, "relaunch_agent_tool_impl")


# ── Deleted file: agent_startup.sh template ──────────────────────────


def test_agent_startup_template_file_is_gone() -> None:
    """The bash startup template used by the spawn path is gone."""
    template = REPO_ROOT / "agent_mcp" / "templates" / "agent_startup.sh"
    assert not template.exists()


# ── env-var plumbing scrubbed from admin_tools.py ────────────────────


def test_mcp_agent_token_env_plumbing_is_gone_from_admin_tools() -> None:
    """The ``MCP_AGENT_TOKEN`` env-var the spawn block exported to
    the claude process is gone from ``admin_tools.py``."""
    src = ADMIN_TOOLS_PY.read_text(encoding="utf-8")
    assert "MCP_AGENT_TOKEN" not in src, (
        "MCP_AGENT_TOKEN env-var plumbing should not appear in "
        "admin_tools.py — the spawn block that set it is deleted."
    )


def test_mcp_system_token_env_plumbing_is_gone_from_admin_tools() -> None:
    """``MCP_SYSTEM_TOKEN`` was retired in retire-system-token Wave 3
    and the spawn block deletion ensures it stays gone."""
    src = ADMIN_TOOLS_PY.read_text(encoding="utf-8")
    assert "MCP_SYSTEM_TOKEN" not in src


# ── tmux primitives no longer referenced in tools ─────────────────────


def test_admin_tools_does_not_reference_tmux_helpers() -> None:
    """``admin_tools.py`` should not name any of the deleted tmux
    helpers — they have no surviving import home."""
    src = ADMIN_TOOLS_PY.read_text(encoding="utf-8")
    for sym in (
        "create_tmux_session",
        "kill_tmux_session",
        "session_exists",
        "send_prompt_async",
        "send_command_to_session",
        "is_tmux_available",
        "list_tmux_sessions",
        "create_agent_session_name",
        "generate_agent_session_name",
        "sanitize_session_name",
    ):
        assert sym not in src, f"admin_tools.py still references {sym}"


def test_task_tools_does_not_reference_tmux_helpers() -> None:
    src = TASK_TOOLS_PY.read_text(encoding="utf-8")
    for sym in (
        "create_tmux_session",
        "kill_tmux_session",
        "send_prompt_async",
        "send_command_to_session",
        "sanitize_session_name",
        "_launch_testing_agent_for_completed_task",
        "_send_escape_to_agent",
    ):
        assert sym not in src, f"task_tools.py still references {sym}"


def test_agent_communication_tools_does_not_reference_tmux_helpers() -> None:
    src = COMM_TOOLS_PY.read_text(encoding="utf-8")
    for sym in (
        "session_exists",
        "send_prompt_async",
        "sanitize_session_name",
    ):
        assert sym not in src, (
            f"agent_communication_tools.py still references {sym}"
        )


# ── globals: agent_tmux_sessions retired ─────────────────────────────


def test_agent_tmux_sessions_global_is_gone() -> None:
    """The in-memory ``agent_id -> session_name`` map is deleted; no
    code writes session names any more."""
    from agent_mcp.core import globals as g
    from agent_mcp.core import state

    assert not hasattr(g, "agent_tmux_sessions")
    assert not hasattr(state, "agent_tmux_sessions")


# ── Routes: legacy POST /api/agents + /api/create-agent are gone ─────


def test_legacy_create_agent_routes_are_not_registered() -> None:
    """The dashboard's spawn-using endpoints (``POST /api/agents`` and
    its back-compat ``/api/create-agent`` alias) are gone. Only the
    register-only ``POST /api/agents/register`` survives."""
    from agent_mcp.app.routes import _dashboard_route_specs

    # Routes entries are 4-tuples (path, handler, methods, name).
    by_path_methods = [
        (entry[0], tuple(entry[2])) for entry in _dashboard_route_specs
    ]

    # GET /api/agents is fine (list endpoint). POST /api/agents must be gone.
    for path, methods in by_path_methods:
        if path == "/api/agents":
            assert "POST" not in methods, (
                "POST /api/agents (spawn) is deleted; the dashboard "
                "uses /api/agents/register only."
            )

    paths = {p for p, _ in by_path_methods}
    assert "/api/create-agent" not in paths, (
        "The /api/create-agent back-compat alias is deleted along with "
        "the spawn handler it pointed at."
    )
    assert "/api/agents/register" in paths, (
        "The register-only endpoint must remain — it is the sole "
        "agent-creation surface."
    )
