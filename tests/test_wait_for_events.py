"""Tests for the `wait_for_events` long-poll tool (plan Phase 2).

Per the locked grilling decisions in
`/home/dennis/.claude/plans/prancy-napping-pie.md`:

* New tool `wait_for_events` returns events for the calling agent
  since an ISO-UTC timestamp cursor. Default 60s wait; server clamps
  to 900s max.
* Event types in v1: `message`, `broadcast`, `task_assigned`,
  `task_changed`. Single timeline ordered by timestamp ASC.
* Fast path: if events already pending for the agent, return
  immediately.
* Wake path: blocks on an `asyncio.Event` keyed by `agent_id`; the
  signal is `.set()` by `send_agent_message`,
  `broadcast_admin_message`, and the task assign/update commits.
* Response envelope: `{"events": [...], "next_cursor": "<ts>"}`
  serialized to a single TextContent.

These tests follow the harness pattern from PR #57. They are the
RED commit per the plan's TDD requirement — they fail before the
implementation lands.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

import pytest
from tests.harness import with_bearer

pytestmark = pytest.mark.asyncio


def _parse_envelope(text: str) -> dict:
    """The tool returns a single TextContent whose `text` is JSON
    `{"events": [...], "next_cursor": "..."}`. Strip leading/trailing
    whitespace and parse."""
    return json.loads(text)


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


async def _wait_for_events_via_admin_session(admin, **arguments):
    """Drive `wait_for_events` through the AdminClient. Returns the
    parsed envelope dict."""
    result = await admin.call("wait_for_events", arguments)
    return _parse_envelope(_content_text(result))


# ---------------------------------------------------------------------------
# Test 1: RED — tool is registered (catches accidental deletion).
# ---------------------------------------------------------------------------


async def test_wait_for_events_is_registered(tmp_path: Path) -> None:
    """The tool name must appear in `tools/list` for both admin and
    workers — every active agent can wait for their own events."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        admin_tools = {t.name for t in await admin.list_tools()}
        assert "wait_for_events" in admin_tools, (
            "admin tools/list missing wait_for_events; "
            f"got: {sorted(admin_tools)}"
        )

        alice = await admin.create_worker("alice")
        worker_tools = {t.name for t in await alice.list_tools()}
        assert "wait_for_events" in worker_tools, (
            "worker tools/list missing wait_for_events; "
            f"got: {sorted(worker_tools)}"
        )


# ---------------------------------------------------------------------------
# Test 2: empty timeout returns an empty events array.
# ---------------------------------------------------------------------------


async def test_empty_timeout_returns_empty_envelope(tmp_path: Path) -> None:
    """With no activity, a short timeout returns
    `{"events": [], "next_cursor": <since-or-now>}`."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = _dt.datetime.now().isoformat()

        start = asyncio.get_event_loop().time()
        env = await _wait_for_events_via_admin_session(
            alice, since=since, timeout_seconds=1
        )
        elapsed = asyncio.get_event_loop().time() - start

        assert env["events"] == [], f"expected no events; got {env}"
        # Cursor is preserved when no events fire.
        assert env["next_cursor"] == since
        # Timed out after ~1s; allow some scheduler slack but stay
        # well under 5s to fail fast on a hung implementation.
        assert elapsed < 5.0, (
            f"wait_for_events should have returned after ~1s timeout; "
            f"took {elapsed:.2f}s"
        )


# ---------------------------------------------------------------------------
# Test 3: fast path — pending events return immediately.
# ---------------------------------------------------------------------------


async def test_fast_path_returns_pending_messages(tmp_path: Path) -> None:
    """Pre-existing messages newer than `since` are returned without
    waiting on the signal."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # Cursor before the message arrives.
        since = (
            _dt.datetime.now() - _dt.timedelta(seconds=1)
        ).isoformat()

        # Admin sends a message to alice — direct, persisted.
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "alice",
                    "message": "hello alice",
                    "deliver_method": "store",
                }
            )

        start = asyncio.get_event_loop().time()
        env = await _wait_for_events_via_admin_session(
            alice, since=since, timeout_seconds=10
        )
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 2.0, (
            f"fast path should return immediately; took {elapsed:.2f}s"
        )
        assert len(env["events"]) == 1, f"want 1 event; got: {env}"
        evt = env["events"][0]
        assert evt["type"] == "message"
        assert evt["data"]["message_content"] == "hello alice"
        assert evt["data"]["sender_id"] == "admin"
        # next_cursor advances past the message timestamp.
        assert env["next_cursor"] >= evt["timestamp"]


# ---------------------------------------------------------------------------
# Test 4: wake-on-message — concurrent waiter wakes within ~1s.
# ---------------------------------------------------------------------------


async def test_wake_on_message_within_one_second(tmp_path: Path) -> None:
    """A `wait_for_events` call already in flight must wake within
    ~1s of `send_agent_message` to the same recipient."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = _dt.datetime.now().isoformat()

        async def waiter():
            return await _wait_for_events_via_admin_session(
                alice, since=since, timeout_seconds=10
            )

        task = asyncio.create_task(waiter())
        # Yield so the waiter actually enters its sleep.
        await asyncio.sleep(0.1)

        start = asyncio.get_event_loop().time()
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "alice",
                    "message": "wake up",
                    "deliver_method": "store",
                }
            )

        env = await asyncio.wait_for(task, timeout=5.0)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 2.0, (
            f"waiter should wake within ~1s of message; took {elapsed:.2f}s"
        )
        assert len(env["events"]) == 1, f"want 1 event; got: {env}"
        assert env["events"][0]["type"] == "message"
        assert env["events"][0]["data"]["message_content"] == "wake up"


# ---------------------------------------------------------------------------
# Test 5: wake-on-broadcast — each active recipient wakes.
# ---------------------------------------------------------------------------


async def test_wake_on_broadcast(tmp_path: Path) -> None:
    """`broadcast_admin_message` writes one message row per active
    recipient; each recipient's waiter wakes with a `broadcast`
    event."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        broadcast_admin_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = _dt.datetime.now().isoformat()

        async def waiter():
            return await _wait_for_events_via_admin_session(
                alice, since=since, timeout_seconds=10
            )

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.1)

        with with_bearer(admin.admin_token):
            await broadcast_admin_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "message": "team announcement",
                }
            )

        env = await asyncio.wait_for(task, timeout=5.0)
        assert len(env["events"]) >= 1
        evt = env["events"][0]
        # The shipped message_type for broadcasts is "broadcast";
        # the event-type discriminator therefore reads "broadcast".
        assert evt["type"] == "broadcast", (
            f"expected broadcast event; got {evt}"
        )
        assert evt["data"]["message_content"] == "team announcement"


# ---------------------------------------------------------------------------
# Test 6: wake-on-task-assign — newly assigned worker's waiter fires.
# ---------------------------------------------------------------------------


async def test_wake_on_task_assigned(tmp_path: Path) -> None:
    """`assign_task` to a worker wakes that worker's waiter with a
    `task_assigned` event (assigned_to transitioned INTO the agent
    since the cursor)."""
    from tests.harness import mcp_session
    from agent_mcp.tools.task_tools import assign_task_tool_impl

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = _dt.datetime.now().isoformat()

        async def waiter():
            return await _wait_for_events_via_admin_session(
                alice, since=since, timeout_seconds=10
            )

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.1)

        with with_bearer(admin.admin_token):
            await assign_task_tool_impl(
                {
                    "token": admin.admin_token,
                    "agent_token": alice.token,
                    "task_title": "Write a sonnet",
                    "task_description": "Iambic pentameter, 14 lines.",
                    "priority": "medium",
                }
            )

        env = await asyncio.wait_for(task, timeout=5.0)
        assert len(env["events"]) == 1, f"want 1 event; got: {env}"
        evt = env["events"][0]
        assert evt["type"] == "task_assigned", (
            f"expected task_assigned; got {evt}"
        )
        assert evt["data"]["assigned_to"] == "alice"


# ---------------------------------------------------------------------------
# Test 7: cursor advances — second call with returned cursor returns
# 0 events until new activity.
# ---------------------------------------------------------------------------


async def test_cursor_advances_between_calls(tmp_path: Path) -> None:
    """Two-call sequence: call 1 returns N events + `next_cursor`;
    call 2 with that cursor returns 0 events (until something new)."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = (
            _dt.datetime.now() - _dt.timedelta(seconds=1)
        ).isoformat()

        # Pre-seed two messages.
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "alice",
                    "message": "one",
                    "deliver_method": "store",
                }
            )
        # Sleep 1ms so timestamps order deterministically.
        await asyncio.sleep(0.01)
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "alice",
                    "message": "two",
                    "deliver_method": "store",
                }
            )

        env1 = await _wait_for_events_via_admin_session(
            alice, since=since, timeout_seconds=2
        )
        assert len(env1["events"]) == 2, f"call 1: got {env1}"
        cursor = env1["next_cursor"]

        # Call 2 with returned cursor → no new activity → empty.
        env2 = await _wait_for_events_via_admin_session(
            alice, since=cursor, timeout_seconds=1
        )
        assert env2["events"] == [], f"call 2 should be empty; got {env2}"


# ---------------------------------------------------------------------------
# Test 8: timeout clamp — values above 900s are clamped.
# ---------------------------------------------------------------------------


async def test_timeout_clamp_to_max(tmp_path: Path) -> None:
    """Passing `timeout_seconds=2000` must clamp server-side to the
    `WAIT_FOR_EVENTS_MAX_TIMEOUT` ceiling.

    PR-2 lowered the ceiling from 900 to 300 (the locked-decisions
    table value); the test now asserts against the module constant
    rather than a bare literal so a future bump doesn't silently
    diverge.

    Verified indirectly: we monkeypatch `asyncio.wait_for` to capture
    the timeout argument the impl passes for the signal-wait call.
    PR-2 also added a per-agent serialization lock that uses
    `asyncio.wait_for(lock.acquire(), timeout=0.001)`; we skip those
    short captures and assert the slice timeout matches the clamped
    ceiling.
    """
    from tests.harness import mcp_session
    import agent_mcp.tools.agent_communication_tools as acm

    captured_timeouts: list[float] = []

    async def spy_wait_for(awaitable, timeout):
        captured_timeouts.append(float(timeout))
        # Lock-acquire calls use a tiny 0.001s timeout. Forward those
        # to the real `asyncio.wait_for` so the lock is actually
        # acquired and the impl progresses to the signal-wait slice.
        if float(timeout) < 0.5:
            return await original(awaitable, timeout)
        # First signal-wait slice captured — that's all this test
        # asserts on. Close the un-awaited coroutine (avoids pytest's
        # "coroutine was never awaited" warning) and return a synthetic
        # event so the impl takes its "woken" path and returns after a
        # SINGLE iteration. Raising TimeoutError here instead made the
        # impl loop until its wall-clock deadline (the post-clamp 300s
        # ceiling) — a ~300s busy-spin that dominated the whole suite.
        try:
            awaitable.close()
        except Exception:
            pass
        return {"type": "test-wake", "timestamp": "z"}

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # Monkeypatch the module's `asyncio` so the impl's call site
        # hits our spy.
        original = acm.asyncio.wait_for
        acm.asyncio.wait_for = spy_wait_for
        try:
            await _wait_for_events_via_admin_session(
                alice, timeout_seconds=2000
            )
        finally:
            acm.asyncio.wait_for = original

        # The PR-2 impl wait_for slices the wait at
        # `_FLAG_RECHECK_INTERVAL_SECONDS` (2s) per iteration so an
        # operator toggling the flag mid-wait wakes the agent within
        # ~5s. We assert the first non-lock-acquire capture equals
        # the slice interval — that's the post-clamp ceiling
        # applied to the signal.wait() call.
        # Filter out the 0.001s lock-acquire captures.
        signal_waits = [t for t in captured_timeouts if t > 0.01]
        assert signal_waits, (
            f"signal.wait timeout never captured; got {captured_timeouts}"
        )
        assert signal_waits[0] == acm._FLAG_RECHECK_INTERVAL_SECONDS, (
            f"first signal slice should equal _FLAG_RECHECK_INTERVAL_SECONDS "
            f"({acm._FLAG_RECHECK_INTERVAL_SECONDS}); "
            f"captured {signal_waits[0]} "
            f"(MAX_TIMEOUT={acm.WAIT_FOR_EVENTS_MAX_TIMEOUT})"
        )


# ---------------------------------------------------------------------------
# Test 9: caller's agent_id is derived from token (no spoofing).
# ---------------------------------------------------------------------------


async def test_caller_agent_id_derived_from_token(tmp_path: Path) -> None:
    """A worker calling `wait_for_events` sees ONLY their own events;
    they cannot pass a different recipient_id to peek at someone
    else's inbox. The tool derives the agent_id from the bearer
    token via `get_agent_id`."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        since = (
            _dt.datetime.now() - _dt.timedelta(seconds=1)
        ).isoformat()

        # Admin sends to bob, not alice.
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "bob",
                    "message": "for bob only",
                    "deliver_method": "store",
                }
            )

        # Alice waits — should NOT see bob's message even though they
        # share the same project DB.
        env = await _wait_for_events_via_admin_session(
            alice, since=since, timeout_seconds=1
        )
        assert env["events"] == [], (
            f"alice should not see bob's mail; got {env}"
        )

        # Bob waits — receives his message via fast path.
        env_bob = await _wait_for_events_via_admin_session(
            bob, since=since, timeout_seconds=1
        )
        assert len(env_bob["events"]) == 1
        assert env_bob["events"][0]["data"]["recipient_id"] == "bob"
