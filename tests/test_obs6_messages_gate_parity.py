"""OBS6 — REST message create routes through the shared send-message gate.

Round-10 class-generalization found that EVERY dashboard REST resource
handler dispatched through its gated MCP tool impl EXCEPT messages:
``create_message_api_route`` wrote the DB ORM-direct after only the
operator-session gate, enforcing none of the gates inside
``send_agent_message_tool_impl`` (the stop_command admin gate, the
worker-to-worker toggle, the per-pair ``_can_agents_communicate`` rules,
and the 4000-char cap).

This is defense-in-depth, not a live-exploit fix: on the wire a
worker/manager bearer → /api/messages is 401 and a cookie viewer is 403,
so only operator/sysadmin cookies (already admin-tier) reach the handler
today. The fix extracts those gates into the shared
``check_send_message_permission`` helper that BOTH the MCP tool and the
REST handler call, so a future router/dep change that lets a lower-tier
principal reach the handler can't silently reopen the gap — the same
one-enforcement-path parity the memories #483 fix established.

These tests pin the gate at the enforcement seam:
  * the shared helper itself enforces each gate (unit), and
  * the REST create path runs through it (the 4000-char cap now rejects
    on REST, and the handler is observed calling the helper), while
  * a legitimate operator sending a normal message still succeeds.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.tool_result import Invalid, PermissionDenied
from agent_mcp.tools.agent_communication_tools import (
    check_send_message_permission,
)
from tests.harness import make_principal, mcp_session


pytestmark = pytest.mark.asyncio


# --- one enforcement path: the tool and the router share the helper ---


async def test_router_and_tool_reference_the_same_gate() -> None:
    """The REST router imports the very same ``check_send_message_permission``
    the MCP tool module defines — structural proof of one enforcement path.
    """
    from agent_mcp.app.routers import messages as messages_router
    from agent_mcp.tools import agent_communication_tools as tool_mod

    assert (
        messages_router.check_send_message_permission
        is tool_mod.check_send_message_permission
    )


# --- shared gate: each tool gate is enforced ---------------------------


async def test_gate_allows_operator_normal_message(tmp_path) -> None:
    async with mcp_session(tmp_path):
        op = make_principal(
            kind="operator_session", user_id="op", project_role="operator"
        )
        denial = check_send_message_permission(
            op, recipient_id="alice", message_content="hi", message_type="text"
        )
        assert denial is None


async def test_gate_denies_worker_to_worker_when_toggle_off(tmp_path) -> None:
    async with mcp_session(tmp_path):
        worker = make_principal(
            kind="agent_bearer", agent_id="w1", agent_role="worker"
        )
        denial = check_send_message_permission(
            worker,
            recipient_id="w2",
            message_content="peer msg",
            message_type="text",
        )
        assert isinstance(denial, PermissionDenied), denial


async def test_gate_denies_stop_command_from_non_admin(tmp_path) -> None:
    """A worker (even with worker-to-worker enabled) cannot send a
    stop_command — that gate is admin-tier only."""
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle("config_allow_worker_to_worker", "true")
        worker = make_principal(
            kind="agent_bearer", agent_id="w1", agent_role="worker"
        )
        denial = check_send_message_permission(
            worker,
            recipient_id="alice",
            message_content="halt",
            message_type="stop_command",
        )
        assert isinstance(denial, PermissionDenied), denial
        assert "stop command" in denial.reason.lower(), denial.reason


async def test_gate_enforces_4000_char_cap_even_for_operator(tmp_path) -> None:
    async with mcp_session(tmp_path):
        op = make_principal(
            kind="operator_session", user_id="op", project_role="operator"
        )
        denial = check_send_message_permission(
            op,
            recipient_id="alice",
            message_content="a" * 4001,
            message_type="text",
        )
        assert isinstance(denial, Invalid), denial


async def test_gate_denies_non_agent_bearer_non_operator(tmp_path) -> None:
    """A forwarding VIEWER (operator_session, viewer project-role) that
    reaches the handler is denied — the defense-in-depth case OBS6 closes."""
    async with mcp_session(tmp_path):
        viewer = make_principal(
            kind="operator_session", user_id="v", project_role="viewer"
        )
        denial = check_send_message_permission(
            viewer, recipient_id="alice", message_content="hi", message_type="text"
        )
        assert isinstance(denial, PermissionDenied), denial


# --- REST enforcement seam --------------------------------------------


async def test_rest_create_enforces_4000_char_cap(tmp_path) -> None:
    """The 4000-char cap is a tool gate the REST create handler did NOT
    enforce before OBS6. It now rejects on REST too (400), proving the
    shared gate is in the REST path."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "a" * 4001,
            },
        )
        assert r.status_code == 400, r.text
        assert "4000" in r.text or "too long" in r.text.lower(), r.text


async def test_rest_create_normal_message_still_succeeds(tmp_path) -> None:
    """Regression: a legitimate operator sending a normal message via
    REST is unchanged — the gate is a no-op for admin-tier operators."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "hello alice",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True, body
        assert "message_id" in body, body


async def test_rest_create_calls_shared_gate(tmp_path, monkeypatch) -> None:
    """The create handler routes through the shared gate for every send —
    observed by spying on the helper the router calls."""
    from agent_mcp.app.routers import messages as messages_router

    calls: list[dict] = []
    real = messages_router.check_send_message_permission

    def _spy(principal, **kwargs):
        calls.append(kwargs)
        return real(principal, **kwargs)

    monkeypatch.setattr(
        messages_router, "check_send_message_permission", _spy
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "gated hello",
            },
        )
        assert r.status_code == 200, r.text
        assert calls, "REST create did not call check_send_message_permission"
        assert calls[0]["recipient_id"] == "alice"
        assert calls[0]["message_content"] == "gated hello"
