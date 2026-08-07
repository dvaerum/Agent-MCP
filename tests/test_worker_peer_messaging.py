"""Worker→worker messaging — gated by config_allow_worker_to_worker.

UPSTREAM_ISSUES.md issue K. Today `send_agent_message` always denies
worker→worker with "Communication not permitted between these agents"
because the policy check in `_can_agents_communicate` (line 54)
inspects `g.active_agents` by agent_id while the dict is actually
keyed by token — so the check never succeeds for non-admin senders.

Fix:
1. Fix the policy lookup to iterate active_agents by agent_id.
2. Gate worker→worker on a per-project `config_allow_worker_to_worker`
   key in project_context (default: deny, preserving upstream behavior
   per Q6b.1).

Migrated to use `tests/harness.py::mcp_session` (Candidate E from
architecture review 2026-06-01). The `_send` helper now drives the
registered MCP framework handler (same as a real SSE client) instead
of invoking `send_agent_message_tool_impl` directly — this also
exercises the tools/list role filter (PR #55), which gates
send_agent_message behind config_allow_worker_to_worker for workers.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


async def test_admin_to_worker_still_works(tmp_path) -> None:
    """Baseline — admin to worker is unaffected by the toggle."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        result = await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "hello from admin",
                "deliver_method": "store",
            },
        )
        text = result[0].text
        assert "denied" not in text.lower(), text


async def test_worker_to_worker_denied_when_toggle_off(tmp_path) -> None:
    """With the toggle EXPLICITLY off, worker→worker is denied (the
    default is now True, so this pins the explicit-off path)."""
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle("config_allow_worker_to_worker", "false")

        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        # NB: with the toggle off, the worker doesn't even see
        # `send_agent_message` in tools/list (PR #55). The framework
        # handler still dispatches the call — the policy check inside
        # the impl is what produces the denial text.
        result = await bob.call(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "hi alice",
                "deliver_method": "store",
            },
        )
        text = result[0].text
        assert "denied" in text.lower() or "not permitted" in text.lower(), (
            f"expected denial; got: {text}"
        )


async def test_worker_to_worker_allowed_by_default(tmp_path) -> None:
    """With NO toggle set, worker→worker is allowed — the default for
    config_allow_worker_to_worker is now True."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        result = await bob.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "hi alice",
                "deliver_method": "store",
            },
        )
        text = result[0].text
        assert (
            "denied" not in text.lower()
            and "not permitted" not in text.lower()
        ), f"expected allow by default; got: {text}"


async def test_worker_to_worker_allowed_when_toggle_on(tmp_path) -> None:
    """With config_allow_worker_to_worker=true, worker→worker allowed."""
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle("config_allow_worker_to_worker", "true")

        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        result = await bob.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "hi alice",
                "deliver_method": "store",
            },
        )
        text = result[0].text
        assert (
            "denied" not in text.lower()
            and "not permitted" not in text.lower()
        ), f"expected allow with toggle on; got: {text}"
