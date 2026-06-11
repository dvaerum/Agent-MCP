"""Contract tests for the ``AgentRuntime`` module (round-2 PR B).

PR series: the round-1 Repository deepening (PRs #146–#155) plus PR A
(``atomic_with_audit``, PR #156) established the precedent that a
~1000-line "utils" module hiding a domain concept should be promoted to
a named module with an honest interface and the old file should become
a re-export shim.

This file pins the contract for the **AgentRuntime** concept — the
machinery that boots, prompts, discovers, and tears down an agent.
Today this lives across ``utils/tmux_utils.py`` (564 lines) and
``utils/worktree_utils.py`` (576 lines); after this PR it lives in
``agent_mcp.runtime.agent_runtime`` and the two utils files are 30–50
line re-export shims that keep existing call sites compiling.

What this test file pins:

* ``agent_mcp.runtime`` package exists and re-exports the
  ``agent_runtime`` module (the convention round-1 used for
  ``agent_mcp.repositories``).
* The class ``AgentRuntime`` is importable from
  ``agent_mcp.runtime.agent_runtime``; the canonical instance is
  exposed as ``agent_mcp.runtime.agent_runtime_instance`` (mirrors
  ``task_repo``/``agent_repo``/``message_repo`` shape).
* The high-level interface (``send_prompt``, ``discover_active``,
  ``is_alive``, ``cleanup``, plus a ``create_worktree`` worktree
  primitive) preserves wire-equivalent semantics to the legacy
  ``send_prompt_async`` / ``discover_active_agents_from_tmux`` /
  ``session_exists`` / ``kill_tmux_session`` / ``create_git_worktree``
  surface.
* The old import paths (``from agent_mcp.utils.tmux_utils import
  send_prompt_async`` etc.) keep working — the shim is the
  PR-#153/#154/#155-canonical thin re-export.
* Tmux/git primitives are exercised through monkeypatched
  ``subprocess.run`` (so tests don't need a real ``tmux`` server or
  ``git`` invocation).

These tests fail on ``main`` because:

* ``agent_mcp.runtime`` does not yet exist as a package.
* ``AgentRuntime`` is not yet a class.
* The shims in ``utils/`` still hold the implementations.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# --- Helpers --------------------------------------------------------------


class FakeProcResult(SimpleNamespace):
    """Stand-in for ``subprocess.CompletedProcess`` with the fields the
    tmux/git wrappers actually read.
    """

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        super().__init__(returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, dispatcher):
    """Install a fake ``subprocess.run`` that routes by the command head.

    ``dispatcher`` is a callable ``(cmd_list) -> FakeProcResult``. We
    patch the *agent_mcp.runtime.agent_runtime* import-time-bound
    name so other modules' ``subprocess`` is left untouched.
    """
    from agent_mcp.runtime import agent_runtime as runtime_mod

    def fake_run(cmd, **kwargs):  # noqa: ANN001 — match subprocess.run sig loosely
        return dispatcher(cmd)

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)


# --- Module layout pinning -----------------------------------------------


def test_runtime_package_imports() -> None:
    """``agent_mcp.runtime`` is a package and exposes the canonical names."""
    import agent_mcp.runtime as runtime_pkg

    # The module form (matches the existing utils-shape API surface).
    assert hasattr(runtime_pkg, "agent_runtime"), (
        "agent_mcp.runtime should expose the agent_runtime submodule "
        "(matches the round-1 'from agent_mcp.repositories import "
        "task_repo' convention)."
    )


def test_agent_runtime_class_exists() -> None:
    """``AgentRuntime`` is a class on ``agent_mcp.runtime.agent_runtime``."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    assert isinstance(AgentRuntime, type), "AgentRuntime should be a class"


def test_agent_runtime_singleton_instance() -> None:
    """A canonical instance is exposed on the module."""
    from agent_mcp.runtime import agent_runtime
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    inst = agent_runtime.get_runtime()
    assert isinstance(inst, AgentRuntime), (
        "agent_runtime.get_runtime() should return an AgentRuntime instance"
    )


# --- Class-level interface contract --------------------------------------


def test_agent_runtime_method_surface() -> None:
    """The AgentRuntime class exposes the documented small interface."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    rt = AgentRuntime()
    for method in (
        "send_prompt",
        "discover_active",
        "is_alive",
        "cleanup",
        "create_worktree",
    ):
        assert callable(getattr(rt, method, None)), (
            f"AgentRuntime should expose method '{method}'"
        )


# --- send_prompt: known/unknown sessions ---------------------------------


def test_send_prompt_to_known_session_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``send_prompt`` returns True when the tmux session exists."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    def dispatcher(cmd):
        head = " ".join(cmd[:3])
        if cmd[:2] == ["tmux", "-V"]:
            return FakeProcResult(0, "tmux 3.3", "")
        if cmd[:2] == ["tmux", "has-session"]:
            return FakeProcResult(0, "", "")  # exists
        if cmd[:2] == ["tmux", "send-keys"]:
            return FakeProcResult(0, "", "")
        return FakeProcResult(0, "", "")

    _patch_subprocess(monkeypatch, dispatcher)

    # Avoid the 3-second sleep in the prompt-send delay
    from agent_mcp.runtime import agent_runtime as runtime_mod

    monkeypatch.setattr(runtime_mod.time, "sleep", lambda _x: None)

    rt = AgentRuntime()
    ok = rt.send_prompt("agent_xyz", "hello world", delay_seconds=0)
    assert ok is True


def test_send_prompt_to_unknown_session_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``send_prompt`` returns False when no such tmux session."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    def dispatcher(cmd):
        if cmd[:2] == ["tmux", "-V"]:
            return FakeProcResult(0, "tmux 3.3", "")
        if cmd[:2] == ["tmux", "has-session"]:
            return FakeProcResult(1, "", "no such session")
        return FakeProcResult(0, "", "")

    _patch_subprocess(monkeypatch, dispatcher)

    from agent_mcp.runtime import agent_runtime as runtime_mod

    monkeypatch.setattr(runtime_mod.time, "sleep", lambda _x: None)

    rt = AgentRuntime()
    ok = rt.send_prompt("ghost_agent", "ping", delay_seconds=0)
    assert ok is False


# --- discover_active ------------------------------------------------------


def test_discover_active_finds_matching_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``discover_active`` returns agent infos for sessions whose names
    match the admin-token suffix pattern.
    """
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    admin_token = "X" * 60 + "abcd"

    def dispatcher(cmd):
        if cmd[:2] == ["tmux", "-V"]:
            return FakeProcResult(0, "tmux 3.3", "")
        if cmd[:2] == ["tmux", "list-sessions"]:
            # Two of-ours + one stranger
            return FakeProcResult(
                0,
                "alice-abcd|1700000000|0|1\n"
                "bob-abcd|1700000001|1|2\n"
                "random_other|1700000002|0|1\n",
                "",
            )
        return FakeProcResult(0, "", "")

    _patch_subprocess(monkeypatch, dispatcher)

    rt = AgentRuntime()
    found = rt.discover_active(admin_token)
    ids = sorted(a["agent_id"] for a in found)
    assert ids == ["alice", "bob"], (
        f"discover_active should find both matching agents, got {ids}"
    )


def test_discover_active_empty_when_no_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``discover_active`` handles the no-server / empty-list path."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    def dispatcher(cmd):
        if cmd[:2] == ["tmux", "-V"]:
            return FakeProcResult(0, "tmux 3.3", "")
        if cmd[:2] == ["tmux", "list-sessions"]:
            return FakeProcResult(1, "", "no server running on /tmp/tmux-1000/default")
        return FakeProcResult(0, "", "")

    _patch_subprocess(monkeypatch, dispatcher)

    rt = AgentRuntime()
    assert rt.discover_active("token_with_suffix_xyzw") == []


# --- is_alive -------------------------------------------------------------


def test_is_alive_true_for_existing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_alive`` returns True when the tmux session exists."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    def dispatcher(cmd):
        if cmd[:2] == ["tmux", "-V"]:
            return FakeProcResult(0, "tmux 3.3", "")
        if cmd[:2] == ["tmux", "has-session"]:
            return FakeProcResult(0, "", "")
        return FakeProcResult(0, "", "")

    _patch_subprocess(monkeypatch, dispatcher)

    rt = AgentRuntime()
    assert rt.is_alive("alice-abcd") is True


def test_is_alive_false_for_missing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_alive`` returns False when no such session."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    def dispatcher(cmd):
        if cmd[:2] == ["tmux", "-V"]:
            return FakeProcResult(0, "tmux 3.3", "")
        if cmd[:2] == ["tmux", "has-session"]:
            return FakeProcResult(1, "", "")
        return FakeProcResult(0, "", "")

    _patch_subprocess(monkeypatch, dispatcher)

    rt = AgentRuntime()
    assert rt.is_alive("ghost-zzzz") is False


# --- cleanup --------------------------------------------------------------


def test_cleanup_kills_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cleanup`` invokes ``tmux kill-session`` for a known session."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    calls: list[list[str]] = []

    def dispatcher(cmd):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "-V"]:
            return FakeProcResult(0, "tmux 3.3", "")
        if cmd[:2] == ["tmux", "has-session"]:
            return FakeProcResult(0, "", "")
        if cmd[:2] == ["tmux", "kill-session"]:
            return FakeProcResult(0, "", "")
        return FakeProcResult(0, "", "")

    _patch_subprocess(monkeypatch, dispatcher)

    rt = AgentRuntime()
    ok = rt.cleanup("alice-abcd")
    assert ok is True
    kill_calls = [c for c in calls if c[:2] == ["tmux", "kill-session"]]
    assert kill_calls, "cleanup should have issued a kill-session call"


def test_cleanup_is_idempotent_on_missing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cleanup`` of a non-existent session is success (idempotent)."""
    from agent_mcp.runtime.agent_runtime import AgentRuntime

    def dispatcher(cmd):
        if cmd[:2] == ["tmux", "-V"]:
            return FakeProcResult(0, "tmux 3.3", "")
        if cmd[:2] == ["tmux", "has-session"]:
            return FakeProcResult(1, "", "")  # no such session
        return FakeProcResult(0, "", "")

    _patch_subprocess(monkeypatch, dispatcher)

    rt = AgentRuntime()
    assert rt.cleanup("never_existed-abcd") is True


# --- Naming policy --------------------------------------------------------


def test_session_naming_policy_round_trip() -> None:
    """``generate_agent_session_name`` + ``parse_agent_session_name``
    round-trip an agent_id through the admin-token suffix."""
    from agent_mcp.runtime.agent_runtime import (
        generate_agent_session_name,
        parse_agent_session_name,
    )

    admin = "tokentokentoken1234"  # last 4 = "1234"
    name = generate_agent_session_name("worker_x", admin)
    parsed = parse_agent_session_name(name, admin)
    assert parsed == "worker_x"


# --- Shim back-compat -----------------------------------------------------


def test_legacy_tmux_utils_shim_reexports() -> None:
    """``agent_mcp.utils.tmux_utils`` continues to expose the legacy
    names — the file becomes a re-export shim, not a deletion.
    """
    from agent_mcp.utils import tmux_utils

    for name in (
        "is_tmux_available",
        "sanitize_session_name",
        "create_tmux_session",
        "session_exists",
        "list_tmux_sessions",
        "kill_tmux_session",
        "send_command_to_session",
        "send_prompt_to_session",
        "send_prompt_async",
        "cleanup_agent_sessions",
        "generate_agent_session_name",
        "parse_agent_session_name",
        "discover_active_agents_from_tmux",
        "sync_agents_from_tmux",
        "get_admin_token_suffix",
        "get_session_status",
    ):
        assert hasattr(tmux_utils, name), (
            f"utils.tmux_utils shim must keep '{name}' for back-compat"
        )


def test_legacy_worktree_utils_shim_reexports() -> None:
    """``agent_mcp.utils.worktree_utils`` keeps its legacy names."""
    from agent_mcp.utils import worktree_utils

    for name in (
        "is_git_repository",
        "get_current_branch",
        "branch_exists",
        "create_git_worktree",
        "list_git_worktrees",
        "has_uncommitted_changes",
        "cleanup_git_worktree",
        "detect_project_setup_commands",
        "run_setup_commands",
        "generate_worktree_path",
        "generate_branch_name",
        "validate_worktree_requirements",
    ):
        assert hasattr(worktree_utils, name), (
            f"utils.worktree_utils shim must keep '{name}' for back-compat"
        )


def test_legacy_shim_routes_to_runtime_module() -> None:
    """Shim functions are the *same callables* as the runtime module's —
    not a re-implementation, but a true re-export."""
    from agent_mcp.runtime import agent_runtime as rt_mod
    from agent_mcp.utils import tmux_utils as shim_t
    from agent_mcp.utils import worktree_utils as shim_w

    assert shim_t.discover_active_agents_from_tmux is rt_mod.discover_active_agents_from_tmux
    assert shim_t.generate_agent_session_name is rt_mod.generate_agent_session_name
    assert shim_w.create_git_worktree is rt_mod.create_git_worktree
    assert shim_w.cleanup_git_worktree is rt_mod.cleanup_git_worktree
