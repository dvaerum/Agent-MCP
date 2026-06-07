"""Fan-out semantics for `wait_for_events` (PR-B / v5.0.24).

Reverses PR #128's "one consumer per agent / HTTP 409" decision. The
real-world dual-use case is a worker's Claude Code MCP session +
a shell-based monitor curling the same bearer — both must be able to
await events, and both must receive each event when it arrives.

Tests cover:
  * Two concurrent waiters with the same bearer both get the event.
  * Neither call is rejected with ``another_wait_in_flight`` / 409.
  * Synthetic events (``unassigned_task_appeared``) are delivered to
    every waiter, not consumed destructively by the first.
  * Single-waiter regression — one waiter, one event still works.
  * Timeout regression — both waiters time out cleanly when no event
    arrives during the window.

The wake path goes through ``g.notify_agent_inbox(agent_id)`` (PR-W2b
EventBus), which all writers already call after commit. We exercise
it via ``send_agent_message`` (DB-backed event) and
``g.notify_unassigned_task_appeared`` (synthetic queue event) to
cover both fan-out paths.
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
# Test 1: two concurrent waiters with the same bearer both receive a
# DB-backed event (message). Neither gets the 409 envelope.
# ---------------------------------------------------------------------------


async def test_two_concurrent_waiters_both_receive_message(
    tmp_path: Path,
) -> None:
    """Worker session + observer session, same bearer. Admin sends one
    message to the worker; both waiters return with the event."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 10}
            )

        first = asyncio.create_task(waiter())
        second = asyncio.create_task(waiter())
        # Give both waiters a slice to enter the slow path before the
        # send wakes them. Without this yield the test still works on
        # fan-out but is flakier on slower CI.
        await asyncio.sleep(0.3)

        # Send a message from admin -> alice via the proper tool path
        # so the writer's notify_agent_inbox fires the EventBus.
        send_result = await admin.call(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "hello fanout",
                "deliver_method": "store",
            },
        )
        assert send_result, "send_agent_message returned no content"

        first_blocks, second_blocks = await asyncio.gather(first, second)

        first_body = _envelope(first_blocks)
        second_body = _envelope(second_blocks)

        # Neither waiter may be rejected with the legacy 409 envelope.
        assert "error" not in first_body, (
            f"first waiter got error envelope: {first_body}"
        )
        assert "error" not in second_body, (
            f"second waiter got error envelope: {second_body}"
        )

        # Both waiters must observe the new message in their events list.
        first_events = first_body.get("events", [])
        second_events = second_body.get("events", [])
        assert first_events, (
            f"first waiter returned no events; body={first_body}"
        )
        assert second_events, (
            f"second waiter returned no events; body={second_body}"
        )

        def _has_message(events: list) -> bool:
            return any(
                "hello fanout" in json.dumps(e) for e in events
            )

        assert _has_message(first_events), (
            f"first waiter missed the message: {first_events}"
        )
        assert _has_message(second_events), (
            f"second waiter missed the message: {second_events}"
        )


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


async def test_synthetic_event_fans_out_to_all_waiters(
    tmp_path: Path,
) -> None:
    """Synthetic events (currently ``unassigned_task_appeared``) are
    queued out-of-band rather than read from a DB row. The pre-fan-out
    impl drained them destructively, so only one waiter saw them. With
    fan-out, every waiter gets its own copy. We drive the notify
    directly via the EventBus so the test is decoupled from the
    capability-matching SQL inside
    ``notify_unassigned_task_appeared`` — the fan-out contract is the
    bus, not the matcher."""
    from agent_mcp.core import event_bus

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 5}
            )

        first = asyncio.create_task(waiter())
        second = asyncio.create_task(waiter())
        await asyncio.sleep(0.3)

        # Synthetic event via the bus — same path the matcher uses
        # after it picks alice out of the capability join.
        event_bus.notify(
            "alice",
            "unassigned_task_appeared",
            {
                "ref_id": "synthetic-task-1",
                "timestamp": "2026-06-07T00:00:00",
                "task_id": "synthetic-task-1",
                "title": "synthetic test task",
                "priority": "normal",
                "required_capabilities": [],
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

        assert _has_synthetic(first_events), (
            f"first waiter missed the synthetic event; got {first_events}"
        )
        assert _has_synthetic(second_events), (
            f"second waiter missed the synthetic event; got {second_events}"
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


async def test_two_waiters_both_time_out_cleanly(tmp_path: Path) -> None:
    """No event during the window — both waiters return empty envelopes
    around the timeout boundary, neither errors."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 1}
            )

        first_blocks, second_blocks = await asyncio.gather(
            waiter(), waiter()
        )

        first_body = _envelope(first_blocks)
        second_body = _envelope(second_blocks)

        assert "error" not in first_body, (
            f"first timeout returned error: {first_body}"
        )
        assert "error" not in second_body, (
            f"second timeout returned error: {second_body}"
        )
        assert first_body.get("events", []) == [], (
            f"first timeout returned non-empty events: {first_body}"
        )
        assert second_body.get("events", []) == [], (
            f"second timeout returned non-empty events: {second_body}"
        )
