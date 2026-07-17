"""Worker-facing send-message denials must be actionable — not terse dead-ends.

A worker driving ``send_agent_message`` hits four denial gates in
:func:`check_send_message_permission` / :func:`_can_agents_communicate`.
The pre-existing wording named the failed policy but offered no working
alternative, so a worker had nothing to do but retry the same call. These
tests pin the improved wording: each denial now points at the escalation
path that actually works (``request_assistance`` / ``add_task_note``).

SECURITY invariants pinned here:
  * The "recipient not active" denial COLLAPSES offline / terminated /
    nonexistent recipients into ONE clause — a worker can never use the
    denial as an existence oracle for another agent id.
  * No recipient/agent id is echoed back in a worker-facing denial.
"""

from __future__ import annotations

from agent_mcp.core.tool_result import PermissionDenied
from agent_mcp.tools import agent_communication_tools as _mod
from agent_mcp.tools.agent_communication_tools import (
    _can_agents_communicate,
    check_send_message_permission,
)
from tests.harness import make_principal


def _worker(agent_id: str = "w1"):
    return make_principal(
        kind="agent_bearer", agent_id=agent_id, agent_role="worker"
    )


# --- #1 worker-to-worker toggle OFF -----------------------------------


def test_worker_to_worker_off_points_at_request_assistance(monkeypatch):
    """Toggle off → name the policy, note it also blocks admins, and offer
    the working escalation (request_assistance) plus the enable path."""
    monkeypatch.setattr(
        _mod._access, "_get_config_bool", lambda *a, **k: False
    )
    denial = check_send_message_permission(
        _worker(),
        recipient_id="w2",
        message_content="hi",
        message_type="text",
    )
    assert isinstance(denial, PermissionDenied), denial
    reason = denial.reason.lower()
    assert "config_allow_worker_to_worker" in reason, denial.reason
    # It blocks messaging admins too — the worker should be told.
    assert "admin" in reason, denial.reason
    # The working alternative + the enable path.
    assert "request_assistance" in reason, denial.reason
    assert "dashboard settings" in reason, denial.reason


# --- #2 recipient not currently active (collapsed, no oracle) ----------


def test_recipient_not_active_collapses_all_cases(monkeypatch):
    """nonexistent, offline, AND terminated recipients yield the SAME
    denial — no existence oracle. Only the sender is active."""
    monkeypatch.setattr(
        _mod._access, "_get_config_bool", lambda *a, **k: True
    )
    monkeypatch.setattr(_mod, "_agents_active_by_id", lambda: {"w1"})

    reasons = []
    for peer in ("never_registered_ghost", "offline_peer", "terminated_peer"):
        allowed, reason = _can_agents_communicate(
            "w1", peer, is_admin=False
        )
        assert allowed is False
        reasons.append(reason)
        # No id leak: the probed recipient id never appears in the denial.
        assert peer not in reason, reason

    # All three collapse to one identical denial string.
    assert len(set(reasons)) == 1, reasons
    collapsed = reasons[0].lower()
    assert "not a currently-active agent" in collapsed, reasons[0]
    assert "offline, terminated, or unknown" in collapsed, reasons[0]


def test_recipient_not_active_worker_facing_message(monkeypatch):
    """Through the worker-facing gate the collapsed denial reads as a full
    'Communication denied: ...' sentence and leaks no recipient id."""
    monkeypatch.setattr(
        _mod._access, "_get_config_bool", lambda *a, **k: True
    )
    monkeypatch.setattr(_mod, "_agents_active_by_id", lambda: {"w1"})
    denial = check_send_message_permission(
        _worker(),
        recipient_id="w2",
        message_content="hi",
        message_type="text",
    )
    assert isinstance(denial, PermissionDenied), denial
    assert "w2" not in denial.reason, denial.reason
    reason = denial.reason.lower()
    assert reason.startswith("communication denied:"), denial.reason
    assert "not a currently-active agent" in reason, denial.reason


# --- #3 stop_command is admin-only ------------------------------------


def test_stop_command_admin_only_offers_text_and_escalation(monkeypatch):
    """A worker sending stop_command (even with the toggle on) is denied
    and told to use a normal 'text' message or request_assistance."""
    monkeypatch.setattr(
        _mod._access, "_get_config_bool", lambda *a, **k: True
    )
    monkeypatch.setattr(_mod, "_agents_active_by_id", lambda: {"w1", "w2"})
    denial = check_send_message_permission(
        _worker(),
        recipient_id="w2",
        message_content="halt",
        message_type="stop_command",
    )
    assert isinstance(denial, PermissionDenied), denial
    reason = denial.reason.lower()
    assert "stop_command" in reason, denial.reason
    assert "admin-only" in reason, denial.reason
    assert "text" in reason, denial.reason
    assert "request_assistance" in reason, denial.reason


# --- #4 self-communication --------------------------------------------


def test_self_communication_points_at_add_task_note(monkeypatch):
    """Messaging yourself is denied with a pointer to add_task_note."""
    monkeypatch.setattr(
        _mod._access, "_get_config_bool", lambda *a, **k: True
    )
    monkeypatch.setattr(_mod, "_agents_active_by_id", lambda: {"w1"})
    denial = check_send_message_permission(
        _worker("w1"),
        recipient_id="w1",
        message_content="note to self",
        message_type="text",
    )
    assert isinstance(denial, PermissionDenied), denial
    reason = denial.reason.lower()
    assert "cannot message yourself" in reason, denial.reason
    assert "add_task_note" in reason, denial.reason
