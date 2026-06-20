"""Tests for the ``wait_for_events_in_flight`` per-agent boolean in
``/api/all-data`` — the dashboard "waiting" chip + Settings page
count consume this field.

Contract (PR-3, updated by PR-B / v5.0.24):
  (i)   Every agent row in /api/all-data carries a
        ``wait_for_events_in_flight: bool`` field.
  (ii)  Defaults to FALSE for every agent when no ``wait_for_events``
        call is currently parked.
  (iii) Returns TRUE while ≥1 ``wait_for_events`` call is in flight
        for that agent (i.e. while ``state.waiter_count(agent_id)``
        > 0). Pre-fan-out this was ``g.lock_for(agent_id).locked()``;
        the lock was retired alongside the per-agent serialization
        decision in PR #128 (see ``docs/adr/0012-wait_for_events_fanout.md``).

The synthetic 'Admin' display row is exempt — admin never enters the
wake loop.
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

        resp = admin.get("/api/all-data")
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

        resp = admin.get("/api/all-data")
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


async def test_wait_in_flight_true_while_waiter_registered(
    tmp_path: Path,
) -> None:
    """While ≥1 ``wait_for_events`` call is parked for an agent the
    /api/all-data row for that agent must report TRUE. Other agents
    stay FALSE.

    We register a fake waiter directly via ``g.register_waiter`` (the
    same entry point ``wait_for_events_tool_impl`` uses on entry).
    The flag is a snapshot of ``g.waiter_count(agent_id) > 0``; if
    the snapshot matches the registry state, the real tool-in-flight
    case is covered by definition.

    PR-B / v5.0.24 swapped the underlying probe from ``lock.locked()``
    to a waiter-count query so multiple concurrent waiters all
    register correctly — the boolean ``wait_for_events_in_flight``
    contract stays the same shape the dashboard already consumed.
    """
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        # Stand in for an actual wait_for_events call — register a
        # queue and leave it parked while we probe the API surface.
        waiter_queue = g.register_waiter("alice")
        try:
            resp = admin.get("/api/all-data")
            assert resp.status_code == 200, resp.text
            agents = resp.json().get("agents", [])
            by_id = {a["agent_id"]: a for a in agents}
            assert "alice" in by_id, sorted(by_id)
            assert "bob" in by_id, sorted(by_id)
            assert by_id["alice"]["wait_for_events_in_flight"] is True, (
                f"alice should be in-flight while a waiter is "
                f"registered; got "
                f"{by_id['alice']['wait_for_events_in_flight']!r}"
            )
            assert by_id["bob"]["wait_for_events_in_flight"] is False, (
                f"bob has no waiter; got "
                f"{by_id['bob']['wait_for_events_in_flight']!r}"
            )

            # Fan-out coverage: a second waiter for the same agent
            # must NOT flip the flag back to False — the count-based
            # probe is "any waiter parked", which covers both single
            # and multi-waiter cases.
            extra_queue = g.register_waiter("alice")
            try:
                resp = admin.get("/api/all-data")
                by_id = {
                    a["agent_id"]: a for a in resp.json().get("agents", [])
                }
                assert by_id["alice"]["wait_for_events_in_flight"] is True, (
                    "two concurrent waiters should still surface TRUE"
                )
            finally:
                g.unregister_waiter("alice", extra_queue)
        finally:
            g.unregister_waiter("alice", waiter_queue)

        # After every waiter unregisters the next /api/all-data call
        # sees the false state.
        resp = admin.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        agents = resp.json().get("agents", [])
        by_id = {a["agent_id"]: a for a in agents}
        assert by_id["alice"]["wait_for_events_in_flight"] is False, (
            "alice should no longer report in-flight after every waiter "
            "deregisters"
        )
