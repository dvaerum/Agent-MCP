"""Manager system prompt must teach the right teammate-messaging tool
and the working-folder-for-notes habit.

Live-system bug: a manager (a Claude Code process connected via MCP)
tried to message its teammates using Claude Code's NATIVE ``SendMessage``
tool and every send failed. Native ``SendMessage`` only reaches native
Task-spawned subagents in the same session, NOT the MCP teammates this
server coordinates. The correct tool is the agent-mcp
``send_agent_message`` MCP tool, addressed by ``recipient_id`` set to the
teammate's ``agent_id`` (e.g. ``pikvm-mcp-server@nixos-developer-system``).

Fix: the generated MANAGER system prompt must

  * teach ``send_agent_message`` (with ``recipient_id``) as the way to
    reach teammates,
  * explicitly warn that native ``SendMessage`` does NOT reach MCP
    teammates,
  * explain that the manager's working folder is its own space for
    notes / progress reports / status logs / scratch work.

These belong to the manager only — a worker prompt must not sprout the
manager coordination block.
"""

from __future__ import annotations

import pytest

from agent_mcp.repositories import get_agent_repo
from agent_mcp.utils.project_utils import generate_system_prompt


@pytest.fixture
def manager_prompt(monkeypatch) -> str:
    repo = get_agent_repo()
    monkeypatch.setattr(
        repo, "get_by_id", lambda agent_id: {"agent_role": "manager"}
    )
    monkeypatch.setattr(
        repo,
        "get_working_directory",
        lambda agent_id: "/home/mgr/coordination-repo",
    )
    return generate_system_prompt(
        agent_id="manager@nixos-developer-system",
        agent_token_for_prompt="tok-mgr",
    )


@pytest.fixture
def worker_prompt(monkeypatch) -> str:
    repo = get_agent_repo()
    monkeypatch.setattr(
        repo, "get_by_id", lambda agent_id: {"agent_role": "worker"}
    )
    monkeypatch.setattr(
        repo, "get_working_directory", lambda agent_id: "/home/wrk/repo"
    )
    return generate_system_prompt(
        agent_id="worker@nixos-developer-system",
        agent_token_for_prompt="tok-wrk",
    )


def test_manager_prompt_teaches_send_agent_message(manager_prompt: str) -> None:
    assert "send_agent_message" in manager_prompt, (
        "manager prompt must teach the agent-mcp send_agent_message tool "
        "for reaching teammates"
    )
    assert "recipient_id" in manager_prompt, (
        "manager prompt must show recipient_id is the teammate's agent_id"
    )


def test_manager_prompt_warns_against_native_sendmessage(
    manager_prompt: str,
) -> None:
    # SendMessage must appear ONLY as the thing NOT to use for teammates.
    assert "SendMessage" in manager_prompt, (
        "manager prompt must name native SendMessage so it can warn "
        "against using it for MCP teammates"
    )
    # The negation must sit right next to the SendMessage mention.
    idx = manager_prompt.index("SendMessage")
    window = manager_prompt[max(0, idx - 60) : idx + 60]
    assert (
        "not" in window.lower()
        or "don't" in window.lower()
        or "do not" in window.lower()
    ), (
        "native SendMessage must be presented as the thing NOT to use for "
        f"teammates; context was: {window!r}"
    )


def test_manager_prompt_teaches_working_folder_for_notes(
    manager_prompt: str,
) -> None:
    lower = manager_prompt.lower()
    assert "notes" in lower, "manager prompt must mention keeping notes"
    assert "progress" in lower, (
        "manager prompt must mention tracking its own progress"
    )
    assert "working" in lower and (
        "folder" in lower or "directory" in lower
    ), "manager prompt must frame the working folder as its own space"


def test_worker_prompt_has_no_manager_coordination_block(
    worker_prompt: str,
) -> None:
    assert "send_agent_message" not in worker_prompt, (
        "the send_agent_message coordination guidance is manager-only"
    )
    assert "recipient_id" not in worker_prompt
