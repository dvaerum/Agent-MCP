"""Delivery scheduler tests (ADR-0021) — the piece that drives the policy
and pushes skinny frames.

evaluate_and_push is exercised against a real worker with a real unread
message: the policy fires, a SKINNY frame (ids/subjects/status, no body) is
pushed onto the worker's live delivery stream. Config is passed in directly
(the pure policy config) so these don't wrestle the settings store; a light
tick() test confirms it reads config + no-ops when disabled by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_mcp.features import delivery_policy as dp
from agent_mcp.features import delivery_scheduler as sched
from agent_mcp.features import delivery_transport as dt
from agent_mcp.tools.agent_communication_tools import (
    send_agent_message_tool_impl,
)
from tests.harness import mcp_session, with_bearer


@pytest.fixture(autouse=True)
def _clean():
    dt.clear()
    sched.clear()
    yield
    dt.clear()
    sched.clear()


def _cfg(**over) -> dp.DeliveryPolicyConfig:
    base = dict(
        enabled=True,
        on_unread_messages=True,
        on_unfinished_tasks=True,
        on_unassigned_tasks=False,
        backoff_initial_seconds=30,
        backoff_max_seconds=3600,
        cooldown_seconds=60,
        wake_dormant=False,
    )
    base.update(over)
    return dp.DeliveryPolicyConfig(**base)


async def _send(admin, to: str, text: str) -> None:
    with with_bearer(admin.admin_token):
        await send_agent_message_tool_impl(
            {
                "token": admin.admin_token,
                "recipient_id": to,
                "message": text,
                "deliver_method": "store",
            }
        )


@pytest.mark.asyncio
async def test_pushes_skinny_frame_on_unread(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await _send(admin, alice.agent_id, "hello alice")
        sub = dt.subscribe(alice.agent_id)  # runtime connected

        assert sched.evaluate_and_push(alice.agent_id, _cfg(), now=100.0) is True
        frame = sub.queue.get_nowait()
        assert frame["type"] == "delivery"
        assert frame["reason"] == "unread_messages"
        assert frame["unread_count"] >= 1
        # SKINNY by SHAPE: each item carries only id/sender/subject — never a
        # full-body field (message/content/body). A short message's text
        # legitimately becomes its derived subject; that's the preview, not
        # a body leak.
        assert frame["unread_messages"], "should list the unread message"
        first = frame["unread_messages"][0]
        assert set(first.keys()) <= {"message_id", "sender_id", "subject"}
        assert not ({"message", "content", "body"} & set(first.keys()))


@pytest.mark.asyncio
async def test_disabled_config_never_pushes(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await _send(admin, alice.agent_id, "hi")
        dt.subscribe(alice.agent_id)
        assert (
            sched.evaluate_and_push(alice.agent_id, _cfg(enabled=False), now=100.0)
            is False
        )


@pytest.mark.asyncio
async def test_working_status_suppresses_push(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await _send(admin, alice.agent_id, "hi")
        dt.subscribe(alice.agent_id)
        dt.set_status(alice.agent_id, "working")
        assert (
            sched.evaluate_and_push(alice.agent_id, _cfg(), now=100.0) is False
        )


@pytest.mark.asyncio
async def test_no_backlog_no_push(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")  # no messages, no tasks
        dt.subscribe(alice.agent_id)
        assert (
            sched.evaluate_and_push(alice.agent_id, _cfg(), now=100.0) is False
        )


@pytest.mark.asyncio
async def test_backoff_prevents_double_push(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await _send(admin, alice.agent_id, "hi")
        dt.subscribe(alice.agent_id)
        cfg = _cfg()
        assert sched.evaluate_and_push(alice.agent_id, cfg, now=100.0) is True
        # 10s later — under the 60s cooldown → no second push.
        assert sched.evaluate_and_push(alice.agent_id, cfg, now=110.0) is False
        # Past the cooldown → pings again.
        assert sched.evaluate_and_push(alice.agent_id, cfg, now=160.0) is True


@pytest.mark.asyncio
async def test_tick_reads_config_and_noops_when_disabled(tmp_path: Path):
    """tick() resolves config from project_settings; delivery is off by
    default, so a tick pushes nothing even with a connected worker + unread."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await _send(admin, alice.agent_id, "hi")
        dt.subscribe(alice.agent_id)
        assert sched.tick(now=100.0) == 0
        assert sched.load_config().enabled is False
