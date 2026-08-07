"""Tests for the event-loop idle-stop wind-down (PR2).

Verifies the ``config_event_idle_stop_seconds`` window ends the wake loop
with a ``stop_listening`` event once an agent has gone that long with no
REAL events — measured across reconnects, reset by every real event, and
disabled entirely when the window is 0.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

import agent_mcp.tools.agent_communication_tools as acm
from agent_mcp.core import client_info_registry

pytestmark = pytest.mark.asyncio


def _parse(result) -> dict:
    from agent_mcp.core.tool_result import render_as_text_content

    return json.loads(render_as_text_content(result)[0].text)


def _set_config(key, value):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        now = _dt.datetime.now().isoformat()
        conn.execute(
            "INSERT INTO project_settings (context_key, value, updated_at, "
            "updated_by) VALUES (?, ?, ?, 'test') "
            "ON CONFLICT(context_key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value), now),
        )
        conn.commit()
    finally:
        conn.close()


def _set_last_activity(agent_id, iso):
    from agent_mcp.repositories import agent_repo

    agent_repo.update_field(agent_id, "last_activity_at", iso)


def _types(env) -> list[str]:
    return [e["type"] for e in env["events"]]


@pytest.fixture(autouse=True)
def _clear_client_info():
    client_info_registry.clear()
    yield
    client_info_registry.clear()


async def test_idle_not_exceeded_returns_empty_not_stop(tmp_path):
    """A fresh agent under the default window holds and returns an empty
    envelope (normal reconnect) — NOT stop_listening — and seeds its
    activity marker."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = _dt.datetime.now().isoformat()

        result = await acm.wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 1},
            principal=alice._principal(),
        )
        env = _parse(result)
        assert env["events"] == []
        assert "stop_listening" not in _types(env)
        # Marker seeded on first listen.
        assert acm._read_last_activity_at(alice.agent_id) is not None


async def test_idle_exceeded_returns_stop_listening(tmp_path):
    """With the window exceeded (last real event long ago), the very next
    call returns stop_listening instead of holding again."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_event_idle_stop_seconds", 1)
        # Last activity 2 hours ago → well past the 1s window.
        old = (_dt.datetime.now() - _dt.timedelta(hours=2)).isoformat()
        _set_last_activity(alice.agent_id, old)
        since = _dt.datetime.now().isoformat()

        start = _dt.datetime.now()
        result = await acm.wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 30},
            principal=alice._principal(),
        )
        elapsed = (_dt.datetime.now() - start).total_seconds()
        env = _parse(result)
        assert _types(env) == ["stop_listening"], env
        assert elapsed < 3.0, f"should stop immediately; took {elapsed:.2f}s"


async def test_idle_zero_never_stops(tmp_path):
    """Window 0 = infinite: even an ancient activity marker never yields
    stop_listening — the agent holds forever (bounded here by an explicit
    short timeout for the test)."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_event_idle_stop_seconds", 0)
        old = (_dt.datetime.now() - _dt.timedelta(days=365)).isoformat()
        _set_last_activity(alice.agent_id, old)
        since = _dt.datetime.now().isoformat()

        result = await acm.wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 1},
            principal=alice._principal(),
        )
        env = _parse(result)
        assert env["events"] == []
        assert "stop_listening" not in _types(env)


async def test_real_event_resets_idle_marker(tmp_path):
    """A real event takes priority over idle-stop AND resets the marker:
    even with an ancient marker and a tiny window, a pending message is
    delivered (not stop_listening) and the marker advances to ~now."""
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )
    from tests.harness import mcp_session, with_bearer

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_event_idle_stop_seconds", 1)
        old = (_dt.datetime.now() - _dt.timedelta(hours=2)).isoformat()
        _set_last_activity(alice.agent_id, old)
        since = (_dt.datetime.now() - _dt.timedelta(seconds=1)).isoformat()

        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": alice.agent_id,
                    "message": "real event",
                    "deliver_method": "store",
                }
            )

        result = await acm.wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 30},
            principal=alice._principal(),
        )
        env = _parse(result)
        assert "message" in _types(env), env
        assert "stop_listening" not in _types(env)
        # Marker advanced to ~now (not the ancient value).
        marker = acm._read_last_activity_at(alice.agent_id)
        marker_dt = _dt.datetime.fromisoformat(marker)
        assert (_dt.datetime.now() - marker_dt).total_seconds() < 10


async def test_idle_stop_fires_during_hold(tmp_path):
    """A holding connection winds down mid-hold: with a 1s window and a
    fresh marker, the call holds ~1s then returns stop_listening (the loop
    idle deadline, not just the pre-hold check)."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_event_idle_stop_seconds", 1)
        # Seed marker to now so the pre-hold check passes and we exercise
        # the in-loop idle deadline.
        _set_last_activity(alice.agent_id, _dt.datetime.now().isoformat())
        since = _dt.datetime.now().isoformat()

        start = _dt.datetime.now()
        result = await acm.wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 30},
            principal=alice._principal(),
        )
        elapsed = (_dt.datetime.now() - start).total_seconds()
        env = _parse(result)
        assert _types(env) == ["stop_listening"], env
        assert 0.5 < elapsed < 4.0, f"should stop ~1s into the hold; got {elapsed:.2f}s"
