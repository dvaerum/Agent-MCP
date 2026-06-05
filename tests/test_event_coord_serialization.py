"""Tests for the per-agent serialization lock on `wait_for_events`.

Spec (PR-2 event-coord): only one concurrent `wait_for_events` call
per agent is allowed. A second concurrent call must return an
HTTP-409-analog error envelope `{"error": "another_wait_in_flight",
"agent_id": ...}` without blocking — the existing call continues
uninterrupted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


async def test_concurrent_wait_returns_conflict_envelope(
    tmp_path: Path,
) -> None:
    """Two concurrent `wait_for_events` calls for the same agent:
    the second returns the conflict envelope immediately while the
    first continues blocking until its own timeout."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 5}
            )

        # Start the first wait. Yield so it actually enters its
        # signal.wait() slice before we issue the second call.
        first = asyncio.create_task(waiter())
        await asyncio.sleep(0.3)

        # Second call should return immediately with conflict.
        start = asyncio.get_event_loop().time()
        second_blocks = await alice.call(
            "wait_for_events", {"timeout_seconds": 5}
        )
        elapsed = asyncio.get_event_loop().time() - start

        # Conflict response should arrive well under 1s — no
        # blocking, no full-timeout wait.
        assert elapsed < 1.0, (
            f"second call should not block; took {elapsed:.2f}s"
        )

        body = json.loads(_content_text(second_blocks))
        assert body.get("error") == "another_wait_in_flight", (
            f"want conflict envelope; got {body}"
        )
        assert body.get("agent_id") == "alice", (
            f"want agent_id in conflict; got {body}"
        )

        # First call should still be running; cancel and clean up so
        # the test teardown doesn't dangle.
        first.cancel()
        try:
            await first
        except (asyncio.CancelledError, BaseException):
            pass


async def test_separate_agents_do_not_serialize(tmp_path: Path) -> None:
    """The lock is per-agent: two different agents can wait
    concurrently. Regression guard against accidentally widening the
    lock scope (e.g. a single global lock)."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        async def waiter(session, timeout):
            return await session.call(
                "wait_for_events", {"timeout_seconds": timeout}
            )

        # Both waiters with the same short timeout — should both
        # return ~simultaneously without one getting a conflict.
        a_task = asyncio.create_task(waiter(alice, 1))
        b_task = asyncio.create_task(waiter(bob, 1))
        a_blocks, b_blocks = await asyncio.gather(a_task, b_task)

        a_body = json.loads(_content_text(a_blocks))
        b_body = json.loads(_content_text(b_blocks))
        # Neither should be a conflict envelope.
        assert "error" not in a_body, f"alice got error: {a_body}"
        assert "error" not in b_body, f"bob got error: {b_body}"
