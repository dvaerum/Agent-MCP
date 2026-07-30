"""Authoritative-Disconnect presence (Option A).

``online`` still tracks real connectivity (parked long-poll OR live SSE
stream OR a poll within the grace window) — EXCEPT a *paused* agent is
reported OFFLINE regardless. "Paused" = the per-agent ``auto_event_loop``
is OFF (operator Disconnect) or the fleet is globally paused.

This is what makes the operator "Disconnect" flip the dot offline the
instant the stream closes — no 150s grace tail — while a normally-working
agent (including an SSE-less daemon that only long-polls) still reads
online.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from tests.harness import mcp_session, seed_agent_rows

pytestmark = pytest.mark.asyncio


def _presence(agent_id, **kw):
    from agent_mcp.app.routers.agents import _mcp_presence_for

    return _mcp_presence_for(agent_id, **kw)


async def test_live_stream_online_when_not_paused(tmp_path) -> None:
    """Baseline: a live SSE stream ⇒ online (multi-signal preserved)."""
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        sid = session_registry.register_session(
            agent_id="alice", bearer_token="__test_seed_alice",
        )
        session_registry.attach_runtime_queue(sid, asyncio.Queue())

        assert _presence(
            "alice", auto_event_loop=True, global_loop_on=True,
        )["online"] is True


async def test_paused_agent_offline_despite_live_stream(tmp_path) -> None:
    """A per-agent-paused agent reads OFFLINE even with a live stream."""
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        sid = session_registry.register_session(
            agent_id="alice", bearer_token="__test_seed_alice",
        )
        session_registry.attach_runtime_queue(sid, asyncio.Queue())

        assert _presence(
            "alice", auto_event_loop=False, global_loop_on=True,
        )["online"] is False


async def test_globally_paused_agent_offline_despite_live_stream(
    tmp_path,
) -> None:
    """A globally-paused fleet reads every agent OFFLINE."""
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        sid = session_registry.register_session(
            agent_id="alice", bearer_token="__test_seed_alice",
        )
        session_registry.attach_runtime_queue(sid, asyncio.Queue())

        assert _presence(
            "alice", auto_event_loop=True, global_loop_on=False,
        )["online"] is False


async def test_recent_poll_online_when_not_paused(tmp_path) -> None:
    """Baseline: a poll within the grace window ⇒ online (the daemon /
    long-poll case, no SSE stream)."""
    async with mcp_session(tmp_path):
        seed_agent_rows("bob")
        now = datetime.datetime.now().isoformat()
        assert _presence(
            "bob", last_activity_at=now, auto_event_loop=True,
            global_loop_on=True,
        )["online"] is True


async def test_paused_agent_no_grace_tail(tmp_path) -> None:
    """THE fix: after Disconnect the agent polled moments ago (within
    grace) but is paused — it must read OFFLINE immediately, no 150s
    grace tail."""
    async with mcp_session(tmp_path):
        seed_agent_rows("bob")
        now = datetime.datetime.now().isoformat()
        assert _presence(
            "bob", last_activity_at=now, auto_event_loop=False,
            global_loop_on=True,
        )["online"] is False
