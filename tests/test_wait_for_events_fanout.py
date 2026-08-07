"""Newest-wins semantics for `wait_for_events`.

An agent must keep exactly ONE event-loop connection. When a new
wait_for_events for an agent registers, it SUPERSEDES any prior parked
waiter for the same agent (which returns a ``connection_superseded``
event and exits) — so an agent can't accumulate stale parked connections
(e.g. a backgrounded call left behind when it reconnects). This replaced
the earlier fan-out (which let N concurrent waiters each receive every
event) after that let backgrounded duplicates pile up.

The waiter mechanism is unchanged; the change is that registering a new
waiter pushes ``WAITER_SUPERSEDE_SENTINEL`` onto the prior ones.

Tests cover:
  * Newest-wins: a second waiter supersedes the first; only the survivor
    receives a subsequently-sent message.
  * No ``another_wait_in_flight`` / 409 on the concurrent call.
  * Synthetic events reach the surviving waiter (not the superseded one).
  * Single-waiter regression — one waiter, one event still works.
  * Two near-simultaneous waiters: one superseded, one survives + times
    out empty.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


def _envelope(blocks) -> dict:
    return json.loads(_content_text(blocks))


# ---------------------------------------------------------------------------
# Test 1: newest-wins — a second wait_for_events for the same agent
# supersedes the first. The older is closed with connection_superseded;
# the newer carries the loop and receives the message.
# ---------------------------------------------------------------------------


async def test_newest_wins_supersedes_prior_waiter(
    tmp_path: Path,
) -> None:
    """An agent must keep exactly ONE event-loop connection: a second
    wait_for_events supersedes the first. The older call returns a
    connection_superseded event (NOT the message); the newer one is the
    sole survivor and receives the subsequently-sent message."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 10}
            )

        first = asyncio.create_task(waiter())
        await asyncio.sleep(0.3)   # first parks
        second = asyncio.create_task(waiter())
        await asyncio.sleep(0.3)   # second registers → supersedes first

        # Only the surviving (newer) waiter should receive this.
        send_result = await admin.call(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "hello newest wins",
                "deliver_method": "store",
            },
        )
        assert send_result, "send_agent_message returned no content"

        first_blocks, second_blocks = await asyncio.gather(first, second)
        first_body = _envelope(first_blocks)
        second_body = _envelope(second_blocks)

        first_types = [e.get("type") for e in first_body.get("events", [])]
        # Older waiter: superseded, and did NOT get the message.
        assert "connection_superseded" in first_types, (
            f"older waiter should be superseded; got {first_body}"
        )
        assert not any(
            "hello newest wins" in json.dumps(e)
            for e in first_body.get("events", [])
        ), f"superseded waiter must NOT get the message; got {first_body}"

        # Newer waiter: sole survivor, receives the message.
        assert any(
            "hello newest wins" in json.dumps(e)
            for e in second_body.get("events", [])
        ), f"surviving waiter should get the message; got {second_body}"


# ---------------------------------------------------------------------------
# Test 2: explicit assertion that the 409 envelope is gone.
# ---------------------------------------------------------------------------


async def test_concurrent_waiter_is_not_rejected_with_409(
    tmp_path: Path,
) -> None:
    """The second concurrent call must NOT return the
    ``another_wait_in_flight`` envelope — fan-out replaces the lock."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 2}
            )

        first = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)

        # Second call: must NOT immediately return the conflict shape.
        # If fan-out is wired correctly, it blocks alongside the first
        # waiter until the shared timeout.
        start = asyncio.get_event_loop().time()
        second_blocks = await alice.call(
            "wait_for_events", {"timeout_seconds": 2}
        )
        elapsed = asyncio.get_event_loop().time() - start

        # The fan-out impl makes this call block on its own slow path
        # until the timeout — proves it isn't taking the fast-conflict
        # exit. A sub-200ms return means the legacy lock is still
        # firing the conflict envelope.
        assert elapsed > 1.0, (
            f"second waiter returned in {elapsed:.2f}s — looks like the "
            f"legacy 409 fast-path is still active"
        )

        second_body = _envelope(second_blocks)
        assert second_body.get("error") != "another_wait_in_flight", (
            f"second waiter got the retired conflict envelope: "
            f"{second_body}"
        )

        # Drain the first waiter so the test teardown is clean.
        first_blocks = await first
        first_body = _envelope(first_blocks)
        assert first_body.get("error") != "another_wait_in_flight"


# ---------------------------------------------------------------------------
# Test 3: synthetic events (per-waiter queue) — both waiters receive a
# fan-out of unassigned_task_appeared, not just the first to drain.
# ---------------------------------------------------------------------------


async def test_synthetic_event_reaches_surviving_waiter(
    tmp_path: Path,
) -> None:
    """Under newest-wins a synthetic event (``unassigned_task_appeared``)
    goes to the sole SURVIVING waiter; the superseded older one exits with
    connection_superseded rather than a duplicate copy. Driven via the
    EventBus directly to decouple from the matcher SQL."""
    from agent_mcp.core import event_bus

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 5}
            )

        first = asyncio.create_task(waiter())
        await asyncio.sleep(0.3)   # first parks
        second = asyncio.create_task(waiter())
        await asyncio.sleep(0.3)   # second supersedes first

        event_bus.notify(
            "alice",
            "unassigned_task_appeared",
            {
                "ref_id": "synthetic-task-1",
                "timestamp": "2026-06-07T00:00:00",
                "task_id": "synthetic-task-1",
                "title": "synthetic test task",
                "priority": "normal",
            },
        )

        first_blocks, second_blocks = await asyncio.gather(first, second)
        first_events = _envelope(first_blocks).get("events", [])
        second_events = _envelope(second_blocks).get("events", [])

        def _has_synthetic(events: list) -> bool:
            return any(
                e.get("type") == "unassigned_task_appeared"
                and (e.get("ref_id") == "synthetic-task-1"
                     or (e.get("payload") or {}).get("task_id")
                     == "synthetic-task-1")
                for e in events
            )

        # Older waiter: superseded (no synthetic copy).
        assert any(
            e.get("type") == "connection_superseded" for e in first_events
        ), f"older waiter should be superseded; got {first_events}"
        # Surviving waiter: receives the synthetic event.
        assert _has_synthetic(second_events), (
            f"surviving waiter missed the synthetic event; got {second_events}"
        )


# ---------------------------------------------------------------------------
# Test 4: single-waiter regression — preserve the v5.0.23 happy path.
# ---------------------------------------------------------------------------


async def test_single_waiter_still_receives_event(tmp_path: Path) -> None:
    """The fan-out refactor must not regress the single-caller case
    that v5.0.23 already supported."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 5}
            )

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)

        await admin.call(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "single waiter ping",
                "deliver_method": "store",
            },
        )

        blocks = await task
        body = _envelope(blocks)
        assert "error" not in body, f"single waiter errored: {body}"
        events = body.get("events", [])
        assert any(
            "single waiter ping" in json.dumps(e) for e in events
        ), f"single waiter missed the message; got {events}"


# ---------------------------------------------------------------------------
# Test 5: both waiters time out cleanly when no event arrives.
# ---------------------------------------------------------------------------


async def test_two_waiters_newest_survives_older_superseded(
    tmp_path: Path,
) -> None:
    """Two near-simultaneous waiters, no event during the window: under
    newest-wins exactly ONE is superseded (connection_superseded) and the
    other (the survivor) times out with an empty envelope. Neither errors.
    Order-agnostic — which of the two wins depends on registration
    scheduling."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 1}
            )

        first_blocks, second_blocks = await asyncio.gather(
            waiter(), waiter()
        )
        bodies = [_envelope(first_blocks), _envelope(second_blocks)]

        for b in bodies:
            assert "error" not in b, f"waiter returned error: {b}"

        superseded = [
            b for b in bodies
            if any(e.get("type") == "connection_superseded"
                   for e in b.get("events", []))
        ]
        empty = [b for b in bodies if b.get("events", []) == []]
        assert len(superseded) == 1, (
            f"exactly one waiter should be superseded; got {bodies}"
        )
        assert len(empty) == 1, (
            f"the survivor should time out empty; got {bodies}"
        )
