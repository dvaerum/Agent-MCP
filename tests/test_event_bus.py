"""Tests for ``agent_mcp.core.event_bus`` (PR-W2b, Finding #1).

The EventBus is the single named seam for agent state notifications.
``bus.notify(agent_id, event_type, payload)`` fans out to a list of
registered adapters; the default adapters preserve the existing
behavior:

* ``LongPollSignalAdapter`` wakes the in-process ``wait_for_events``
  blocking ``asyncio.Event`` (and appends synthetic events to the
  per-agent queue).
* ``StreamingQueueAdapter`` enqueues a notification payload onto every
  GET /mcp streaming queue registered for the agent.
* ``AuditLogAdapter`` is a no-op placeholder for future sinks (env-gated
  debug log).

The legacy entry points ``state.notify_agent_inbox`` and
``state.notify_unassigned_task_appeared`` keep their existing
signatures but route through the bus, so every existing call site
continues to wake long-poll waiters and streaming subscribers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Test A: an adapter registered with the bus receives notify() calls.
# ---------------------------------------------------------------------------


def test_register_adapter_receives_notify_call() -> None:
    """A registered adapter's ``deliver`` is invoked with the right args."""
    from agent_mcp.core import event_bus

    received: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []

    class FakeAdapter:
        def deliver(
            self,
            agent_id: str,
            event_type: str,
            payload: Optional[Dict[str, Any]],
        ) -> None:
            received.append((agent_id, event_type, payload))

    event_bus.register("fake", FakeAdapter())
    try:
        event_bus.notify("agent-x", "test_event", {"k": 1})
    finally:
        event_bus.unregister("fake")

    assert received == [("agent-x", "test_event", {"k": 1})], (
        f"adapter did not receive expected call: {received}"
    )


# ---------------------------------------------------------------------------
# Test B: two adapters both receive each call, in registration order.
# ---------------------------------------------------------------------------


def test_multiple_adapters_each_receive_call_in_order() -> None:
    from agent_mcp.core import event_bus

    order: List[str] = []

    class TaggingAdapter:
        def __init__(self, name: str) -> None:
            self.name = name

        def deliver(
            self,
            agent_id: str,
            event_type: str,
            payload: Optional[Dict[str, Any]],
        ) -> None:
            order.append(self.name)

    event_bus.register("first", TaggingAdapter("first"))
    event_bus.register("second", TaggingAdapter("second"))
    try:
        event_bus.notify("agent-x", "ping", None)
    finally:
        event_bus.unregister("first")
        event_bus.unregister("second")

    assert order == ["first", "second"], (
        f"adapters fired in unexpected order: {order}"
    )


# ---------------------------------------------------------------------------
# Test C: a crashing adapter does not block subsequent adapters.
# ---------------------------------------------------------------------------


def test_crashing_adapter_does_not_break_bus_or_other_adapters() -> None:
    from agent_mcp.core import event_bus

    survivor_called: List[bool] = []

    class CrashingAdapter:
        def deliver(
            self,
            agent_id: str,
            event_type: str,
            payload: Optional[Dict[str, Any]],
        ) -> None:
            raise RuntimeError("kaboom — adapter is unhappy")

    class SurvivorAdapter:
        def deliver(
            self,
            agent_id: str,
            event_type: str,
            payload: Optional[Dict[str, Any]],
        ) -> None:
            survivor_called.append(True)

    event_bus.register("crash", CrashingAdapter())
    event_bus.register("survivor", SurvivorAdapter())
    try:
        # Must not raise — bus catches per-adapter exceptions and logs them.
        event_bus.notify("agent-x", "ping", {})
    finally:
        event_bus.unregister("crash")
        event_bus.unregister("survivor")

    assert survivor_called == [True], (
        "survivor adapter was not invoked after the crashing one threw"
    )


# ---------------------------------------------------------------------------
# Test D: regression — state.notify_agent_inbox still wakes signal_for waiters
# (LongPollSignalAdapter path).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_agent_inbox_still_wakes_signal_for_waiters() -> None:
    """The bus-routed shim must still wake the long-poll signal."""
    from agent_mcp.core import state

    # Fresh signal in the per-loop dict.
    state.agent_event_signals.pop("agent-y", None)
    sig = state.signal_for("agent-y")
    assert not sig.is_set()

    # Block a waiter task.
    waiter_task = asyncio.create_task(sig.wait())
    # Yield so the waiter is parked on the Event before we fire.
    await asyncio.sleep(0)

    state.notify_agent_inbox("agent-y")

    # Should resolve quickly.
    try:
        await asyncio.wait_for(waiter_task, timeout=1.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "notify_agent_inbox failed to wake a waiter blocked on "
            "signal_for(agent-y) — the LongPollSignalAdapter path is broken."
        )

    assert sig.is_set()


# ---------------------------------------------------------------------------
# Test E: regression — state.notify_agent_inbox still pushes to
# session_registry.fanout_to_agent (StreamingQueueAdapter path).
# ---------------------------------------------------------------------------


def test_notify_agent_inbox_still_pushes_to_session_registry_fanout() -> None:
    from agent_mcp.core import session_registry, state

    calls: List[Tuple[str, Any]] = []

    def _spy(agent_id: str, payload: Any):  # mirrors fanout_to_agent sig
        calls.append((agent_id, payload))
        return []

    with patch.object(session_registry, "fanout_to_agent", _spy):
        state.notify_agent_inbox("agent-z")

    assert len(calls) == 1, (
        f"expected exactly one fanout_to_agent call; got {calls}"
    )
    agent_id, payload = calls[0]
    assert agent_id == "agent-z"
    assert isinstance(payload, dict)
    # The payload contract today is a JSON-RPC notifications/resources/updated
    # envelope. Pin the agent-scoped URI so downstream consumers don't drift.
    assert payload.get("method") == "notifications/resources/updated"
    params = payload.get("params") or {}
    assert params.get("uri") == "agent-mcp://inbox/agent-z"


# ---------------------------------------------------------------------------
# Test F: AuditLogAdapter is wired but defaults to no-op (env-gated).
# ---------------------------------------------------------------------------


def test_audit_log_adapter_registered_by_default() -> None:
    """The audit adapter must be in the registry so the pattern is
    exercised, even when its DEBUG log is gated off."""
    from agent_mcp.core import event_bus

    names = {name for name, _ in event_bus._adapters}  # noqa: SLF001
    assert "AuditLogAdapter" in names, (
        f"AuditLogAdapter not registered; got adapters: {names}"
    )
    assert "LongPollSignalAdapter" in names, (
        f"LongPollSignalAdapter not registered; got adapters: {names}"
    )
    assert "StreamingQueueAdapter" in names, (
        f"StreamingQueueAdapter not registered; got adapters: {names}"
    )


# ---------------------------------------------------------------------------
# Test G: notify_unassigned_task_appeared shim still routes via the bus.
# ---------------------------------------------------------------------------


def test_notify_unassigned_task_appeared_routes_through_bus(
    tmp_path,
) -> None:
    """The capability-matched fanout still uses the bus per-agent."""
    from agent_mcp.core import event_bus, state

    received: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []

    class CaptureAdapter:
        def deliver(
            self,
            agent_id: str,
            event_type: str,
            payload: Optional[Dict[str, Any]],
        ) -> None:
            received.append((agent_id, event_type, payload))

    # Stand in for the DB so the impl can hand us controlled rows.
    class FakeRow(dict):
        def __getitem__(self, key):  # sqlite Row-like indexing
            return dict.__getitem__(self, key)

    class FakeCursor:
        def __init__(self) -> None:
            self._next: List[Any] = []

        def execute(self, sql: str, params=()):
            if "FROM tasks" in sql:
                self._next = [
                    FakeRow(
                        task_id="task-1",
                        title="Do thing",
                        priority="low",
                        required_capabilities="[]",
                        created_at="2026-06-05T00:00:00",
                    )
                ]
            else:
                # agents row
                self._next = [
                    FakeRow(agent_id="alice", capabilities="[]"),
                    FakeRow(agent_id="admin", capabilities="[]"),
                ]
            return self

        def fetchone(self):
            return self._next[0] if self._next else None

        def fetchall(self):
            return self._next

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    from agent_mcp.db import connection as _connection

    event_bus.register("capture", CaptureAdapter())
    try:
        with patch.object(_connection, "get_db_connection", lambda: FakeConn()):
            state.notify_unassigned_task_appeared("task-1", [])
    finally:
        event_bus.unregister("capture")

    # alice should have received the bus event; admin is excluded.
    matched = [
        (aid, etype) for aid, etype, _payload in received
        if etype == "unassigned_task_appeared"
    ]
    assert ("alice", "unassigned_task_appeared") in matched, (
        f"expected an unassigned_task_appeared bus event for alice; "
        f"got {received}"
    )
    assert all(aid != "admin" for aid, _ in matched), (
        f"admin should not receive unassigned_task_appeared; got {received}"
    )
