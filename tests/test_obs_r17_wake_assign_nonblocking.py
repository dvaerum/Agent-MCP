"""OBS-R17-WAKE regression guard: assigning a task to an OFFLINE agent
must never block the caller.

Observation (pentest, 2026-08-10)
---------------------------------
Over the stateless streamable-HTTP MCP wire, ``assign_task`` sometimes
returned on the first call but, on repeats, left the SSE stream emitting
only ``:ping`` heartbeats and never delivered the result within 25s. The
hypothesis under investigation: the post-commit wake/notify to the
assignee's inbox is done SYNCHRONOUSLY, so when the assignee has no live
``wait_for_events`` listener and no GET-/mcp session, the *caller's* own
response would block.

Verdict: BENIGN (client/transport artifact, not a server caller-block)
----------------------------------------------------------------------
The whole notify path is synchronous and non-blocking end to end:

  ``g.notify_agent_inbox``  (plain ``def`` returning ``None``)
    → ``event_bus.notify``  (for-loop over adapters, each try/except)
      → ``LongPollSignalAdapter.deliver``
            ``state.signal_for(id).set()``      (Event.set — non-blocking)
            ``state.notify_waiters(id)``        (returns iff no waiters;
                                                 else ``put_nowait``)
      → ``StreamingQueueAdapter.deliver``
            ``session_registry.fanout_to_agent`` (fast SQLite SELECT +
                                                  ``put_nowait`` / drop-on-full)
      → ``AuditLogAdapter.deliver``             (no-op unless env-gated)

Nothing in that chain ``await``s, acquires a blocking lock, or waits on a
queue. With NO listener and NO session every step is a no-op / empty
fan-out. The observed SSE park is therefore on the client / transport
side of the stateless streamable-HTTP wire, NOT the server's
``assign_task`` response path.

These tests are the permanent regression guard for that verdict: they
prove assign-to-offline-agent never blocks the caller AND that delivery
is still preserved when a listener DOES exist (so a future refactor
can't "fix" a non-bug by breaking R13/R14 delivery).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time

import pytest

import agent_mcp.core.globals as g_mod
from agent_mcp.core import event_bus, session_registry, state
from tests.harness import mcp_session

# NB: no module-level ``pytestmark = pytest.mark.asyncio`` — the
# structural test below is intentionally synchronous, so each async test
# carries its own marker instead of blanket-marking the sync one.

# Generous vs the 25s SSE-park hypothesis, tight enough to catch a real
# caller-block: the harness assign path completes in well under 1s.
_BOUND_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Structural: the notify path CANNOT await — every hop is a plain ``def``.
# ---------------------------------------------------------------------------


def test_notify_path_is_synchronous_never_a_coroutine() -> None:
    """``notify_agent_inbox`` and every default EventBus adapter's
    ``deliver`` must be plain synchronous callables. A coroutine anywhere
    on this path would mean the writer either awaits it (block risk) or
    silently drops the notification — both regressions."""
    assert not inspect.iscoroutinefunction(g_mod.notify_agent_inbox), (
        "notify_agent_inbox became a coroutine — writers call it "
        "synchronously and would leak an un-awaited coroutine (or block)."
    )
    assert not inspect.iscoroutinefunction(state.notify_agent_inbox)

    adapters = dict(event_bus._adapters)
    assert {
        "LongPollSignalAdapter",
        "StreamingQueueAdapter",
        "AuditLogAdapter",
    } <= set(adapters), f"default adapters missing: {sorted(adapters)}"
    for name, adapter in adapters.items():
        assert not inspect.iscoroutinefunction(adapter.deliver), (
            f"adapter {name!r}.deliver became a coroutine — the bus "
            "invokes it synchronously and never awaits."
        )


# ---------------------------------------------------------------------------
# No listener + no session: notify is a prompt no-op and mints no state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_to_offline_agent_returns_promptly_and_creates_no_waiter(
    tmp_path,
) -> None:
    """``notify_agent_inbox`` for an agent with NO ``wait_for_events``
    waiter and NO GET-/mcp session returns ``None`` essentially instantly
    and does not register a waiter queue as a side effect."""
    async with mcp_session(tmp_path):
        ghost = "ghost-agent-never-connected"
        assert state.waiter_count(ghost) == 0

        start = time.perf_counter()
        result = g_mod.notify_agent_inbox(ghost)
        elapsed = time.perf_counter() - start

        assert result is None, "notify_agent_inbox must return None"
        assert not inspect.isawaitable(result), (
            "notify_agent_inbox returned an awaitable — the path can await"
        )
        assert elapsed < 0.5, (
            f"notify to an offline agent took {elapsed:.3f}s — the notify "
            "path is blocking when it should be a no-op fan-out."
        )
        # Notifying must not conjure a waiter/session out of thin air.
        assert state.waiter_count(ghost) == 0
        assert session_registry.sessions_for_agent(ghost) == []


# ---------------------------------------------------------------------------
# R14 guard: a full streaming queue is DROPPED, never blocks the caller.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fanout_does_not_block_on_full_streaming_queue(tmp_path) -> None:
    """A slow/stuck GET-/mcp subscriber whose runtime queue is already
    full must not block the writer: ``fanout_to_agent`` drops the payload
    (R14 bounded-queue drop-on-full) and returns promptly."""
    async with mcp_session(tmp_path) as admin:
        stuck = await admin.create_worker("stuck-subscriber")
        session_id = session_registry.register_session(
            agent_id=stuck.agent_id, bearer_token=stuck.token
        )
        full_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait("occupied")  # now full
        session_registry.attach_runtime_queue(session_id, full_queue)
        try:
            start = time.perf_counter()
            delivered = session_registry.fanout_to_agent(
                stuck.agent_id, {"jsonrpc": "2.0", "method": "x"}
            )
            elapsed = time.perf_counter() - start

            assert elapsed < 0.5, (
                f"fanout blocked {elapsed:.3f}s on a full queue — "
                "drop-on-full (R14) regressed to a blocking put."
            )
            # Nothing delivered (dropped), queue unchanged at its cap.
            assert delivered == []
            assert full_queue.qsize() == 1
        finally:
            session_registry.detach_runtime_queue(session_id)
            session_registry.unregister_session(session_id)


# ---------------------------------------------------------------------------
# Delivery-preserved guard: when a listener EXISTS, notify still delivers.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_still_delivers_when_a_listener_is_present(
    tmp_path,
) -> None:
    """The non-block guarantee must NOT be bought by dropping delivery: a
    live GET-/mcp subscriber (attached runtime queue) still receives the
    ``notifications/resources/updated`` envelope on notify."""
    async with mcp_session(tmp_path) as admin:
        listener = await admin.create_worker("live-listener")
        session_id = session_registry.register_session(
            agent_id=listener.agent_id, bearer_token=listener.token
        )
        queue: asyncio.Queue = asyncio.Queue()
        session_registry.attach_runtime_queue(session_id, queue)
        try:
            g_mod.notify_agent_inbox(listener.agent_id)

            envelope = queue.get_nowait()
            assert envelope["method"] == "notifications/resources/updated"
            assert (
                envelope["params"]["uri"]
                == f"agent-mcp://inbox/{listener.agent_id}"
            )
        finally:
            session_registry.detach_runtime_queue(session_id)
            session_registry.unregister_session(session_id)


# ---------------------------------------------------------------------------
# Integration: assign_task to an OFFLINE agent returns promptly.
# ---------------------------------------------------------------------------


def _spy_on_notify(monkeypatch) -> list[str]:
    """Record every ``g.notify_agent_inbox`` recipient while preserving the
    real (synchronous) behaviour so the delivery path still runs."""
    real = g_mod.notify_agent_inbox
    recorded: list[str] = []

    def _spy(agent_id: str):
        recorded.append(agent_id)
        return real(agent_id)

    monkeypatch.setattr(g_mod, "notify_agent_inbox", _spy)
    return recorded


@pytest.mark.asyncio
async def test_assign_task_to_offline_agent_returns_promptly(
    tmp_path, monkeypatch
) -> None:
    """``assign_task`` targeting an agent with NO ``wait_for_events``
    listener and NO GET-/mcp session must complete well under the bound,
    with the post-commit inbox wake fired for the offline assignee."""
    # This test's _BOUND_SECONDS budget covers the wake mechanism, not
    # RAG placement-analysis latency — assign_task's create-and-assign
    # path otherwise runs a real (network-dependent) RAG call first,
    # which can push completion past the bound under load and fail this
    # test for a reason unrelated to wake events. Same pattern as
    # test_sec_r3_task_cache.py.
    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.ENABLE_TASK_PLACEMENT_RAG", False
    )

    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob-offline")
        assert state.waiter_count(bob.agent_id) == 0
        assert session_registry.sessions_for_agent(bob.agent_id) == []

        recorded = _spy_on_notify(monkeypatch)

        result = await asyncio.wait_for(
            admin.assert_tool_succeeds(
                "assign_task",
                {
                    "agent_token": bob.token,
                    "task_title": "offline assign",
                    "task_description": "no listener, no session",
                },
            ),
            timeout=_BOUND_SECONDS,
        )
        assert re.search(r"task_[a-f0-9]+", result[0].text), result[0].text
        assert bob.agent_id in recorded, (
            f"post-commit inbox wake not fired for the offline assignee; "
            f"notified={recorded}"
        )


@pytest.mark.asyncio
async def test_bulk_reassign_to_offline_agent_returns_promptly(
    tmp_path, monkeypatch
) -> None:
    """Bulk reassign (``bulk_task_operations`` → ``_wake_task_assignees``)
    onto an offline agent must also complete under the bound and wake the
    new assignee, without blocking on the absent listener."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice-offline")
        carol = await admin.create_worker("carol-offline")

        created = await admin.assert_tool_succeeds(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "reassign target",
                "task_description": "starts on alice",
            },
        )
        task_id = re.search(r"task_[a-f0-9]+", created[0].text).group(0)

        assert session_registry.sessions_for_agent(carol.agent_id) == []
        recorded = _spy_on_notify(monkeypatch)

        result = await asyncio.wait_for(
            admin.assert_tool_succeeds(
                "bulk_task_operations",
                {
                    "operations": [
                        {
                            "type": "reassign",
                            "task_id": task_id,
                            "assigned_to": carol.agent_id,
                        }
                    ]
                },
            ),
            timeout=_BOUND_SECONDS,
        )
        assert "reassign" in result[0].text.lower() or "success" in (
            result[0].text.lower()
        ), result[0].text
        assert carol.agent_id in recorded, (
            f"bulk reassign did not wake the new (offline) assignee; "
            f"notified={recorded}"
        )
