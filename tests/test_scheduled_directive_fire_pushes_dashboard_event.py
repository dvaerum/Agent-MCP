"""Scheduled-directive fires must push a dashboard live-update event —
the same choke point every other mutation already goes through
(log_agent_action_to_db -> _push_dashboard_data_changed). Covers the
wait-loop-native collector directly; the ADR-0026 delivery-transport
trigger shares the same collect_due_and_fire + logging call so is
covered by construction.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent_mcp.core.tool_result import Ok
from agent_mcp.features import operator_events
from agent_mcp.tools.agent_communication_tools import (
    _collect_scheduled_directive_events_for,
)
from agent_mcp.tools.scheduled_directive_tools import (
    create_scheduled_directive_tool_impl,
)
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_subscribers():
    before = list(operator_events._subscribers)
    operator_events._subscribers.clear()
    yield
    operator_events._subscribers.clear()
    operator_events._subscribers.extend(before)


async def test_scheduled_directive_fire_pushes_dashboard_event(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        res = await create_scheduled_directive_tool_impl(
            {"prompt": "check status", "interval_seconds": 60, "run_now": True},
            principal=alice._principal(),
        )
        assert isinstance(res, Ok), res

        sub = operator_events.subscribe()

        events = _collect_scheduled_directive_events_for(alice.agent_id)
        assert events, "expected the run_now directive to fire immediately"

        payload = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
        assert payload["method"] == "notifications/resources/updated"
        assert "schedule" in json.dumps(payload)
