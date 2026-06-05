"""RED tests for PR-3 of the event-coord plan: surface the
``wait_for_events_in_flight`` per-agent boolean in /api/all-data so
the dashboard can render the "waiting" chip + Settings page count.

Contract:
  (i)   Every agent row in /api/all-data carries a
        ``wait_for_events_in_flight: bool`` field.
  (ii)  Defaults to FALSE for every agent when no per-agent
        long-poll lock is held.
  (iii) Returns TRUE while a `wait_for_events` call is in flight for
        that agent (i.e. while ``g.lock_for(agent_id)`` is held).

The PR-2 hardening uses ``g.lock_for(agent_id)`` (an asyncio.Lock)
to enforce one-call-per-agent on `wait_for_events`. The dashboard
surface just exposes ``lock.locked()`` per agent under the agent's
JSON object. The synthetic 'Admin' display row is exempt — admin
never enters the wake loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_all_data_agents_include_wait_in_flight_field(
    tmp_path: Path,
) -> None:
    """Every agent (including the synthetic Admin entry) carries the
    boolean field even when no wait is in flight."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        resp = admin.client.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        agents = resp.json().get("agents", [])
        assert agents, "expected at least one agent row"
        for agent in agents:
            assert "wait_for_events_in_flight" in agent, (
                f"missing wait_for_events_in_flight on agent "
                f"{agent.get('agent_id')!r}: keys={sorted(agent.keys())}"
            )
            assert isinstance(agent["wait_for_events_in_flight"], bool), (
                f"wait_for_events_in_flight must be a bool; got "
                f"{type(agent['wait_for_events_in_flight']).__name__}"
            )


async def test_wait_in_flight_defaults_false_when_no_lock_held(
    tmp_path: Path,
) -> None:
    """Newly-registered workers have no in-flight wait_for_events call —
    every row must report FALSE."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        resp = admin.client.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        agents = resp.json().get("agents", [])
        in_flight = {
            a["agent_id"]: a.get("wait_for_events_in_flight")
            for a in agents
        }
        for agent_id, value in in_flight.items():
            assert value is False, (
                f"agent {agent_id!r} expected wait_for_events_in_flight=False "
                f"with no lock held; got {value!r}"
            )


async def test_wait_in_flight_true_while_lock_held(tmp_path: Path) -> None:
    """While a `wait_for_events` call holds the per-agent lock, the
    /api/all-data row for that agent must report TRUE. Other agents
    stay FALSE.

    We acquire the lock directly via ``g.lock_for(agent_id)`` rather
    than calling the tool itself — simulates the in-flight state
    without needing to also drive a concurrent task. The flag is
    purely a snapshot of ``lock.locked()``; if the snapshot matches
    the lock state, the tool-in-flight case is covered by definition.
    """
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        lock = g.lock_for("alice")
        await lock.acquire()
        try:
            resp = admin.client.get("/api/all-data")
            assert resp.status_code == 200, resp.text
            agents = resp.json().get("agents", [])
            by_id = {a["agent_id"]: a for a in agents}
            assert "alice" in by_id, sorted(by_id)
            assert "bob" in by_id, sorted(by_id)
            assert by_id["alice"]["wait_for_events_in_flight"] is True, (
                f"alice should be in-flight while her lock is held; "
                f"got {by_id['alice']['wait_for_events_in_flight']!r}"
            )
            assert by_id["bob"]["wait_for_events_in_flight"] is False, (
                f"bob's lock is not held; got "
                f"{by_id['bob']['wait_for_events_in_flight']!r}"
            )
        finally:
            lock.release()

        # After release the next /api/all-data call sees the false state.
        resp = admin.client.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        agents = resp.json().get("agents", [])
        by_id = {a["agent_id"]: a for a in agents}
        assert by_id["alice"]["wait_for_events_in_flight"] is False, (
            "alice should no longer report in-flight after lock release"
        )
