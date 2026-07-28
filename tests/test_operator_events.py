"""In-process operator-events hub — the dashboard SSE fan-out that
operators (NOT agents) subscribe to.

The agent-scoped ``core.session_registry`` persists each stream as an
``mcp_sessions`` row whose ``agent_id`` has a DDL-level FK into
``agents``; an operator has no such row and cannot register there. This
hub is the operator-side equivalent — a dependency-light, in-memory-only
pub/sub the ``GET /api/events`` endpoint reads and the mutation choke
point (``_push_dashboard_data_changed``) publishes onto.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_mcp.features import operator_events


@pytest.fixture(autouse=True)
def _clear_subscribers():
    """Each test starts from a clean subscriber set (module-level state)."""
    # Snapshot + restore so a test can't leak a queue into the next.
    before = list(operator_events._subscribers)
    operator_events._subscribers.clear()
    yield
    operator_events._subscribers.clear()
    operator_events._subscribers.extend(before)


def test_subscribe_returns_queue_and_counts():
    assert operator_events.subscriber_count() == 0
    q = operator_events.subscribe()
    assert isinstance(q, asyncio.Queue)
    assert operator_events.subscriber_count() == 1


def test_publish_reaches_subscribed_queue():
    q = operator_events.subscribe()
    payload = {"jsonrpc": "2.0", "method": "notifications/resources/updated"}
    operator_events.publish(payload)
    assert q.get_nowait() is payload


def test_publish_fans_out_to_all_subscribers():
    q1 = operator_events.subscribe()
    q2 = operator_events.subscribe()
    assert operator_events.subscriber_count() == 2
    payload = {"hello": "world"}
    operator_events.publish(payload)
    assert q1.get_nowait() is payload
    assert q2.get_nowait() is payload


def test_publish_with_no_subscribers_does_not_raise():
    # Best-effort telemetry: publishing into the void is a no-op.
    operator_events.publish({"anything": True})


def test_unsubscribe_removes_and_is_idempotent():
    q = operator_events.subscribe()
    assert operator_events.subscriber_count() == 1
    operator_events.unsubscribe(q)
    assert operator_events.subscriber_count() == 0
    # Idempotent: a second unsubscribe (or one for an unknown queue) is
    # a silent no-op, never a KeyError/ValueError.
    operator_events.unsubscribe(q)
    operator_events.unsubscribe(asyncio.Queue())
    assert operator_events.subscriber_count() == 0


def test_unsubscribed_queue_receives_nothing():
    q = operator_events.subscribe()
    operator_events.unsubscribe(q)
    operator_events.publish({"x": 1})
    assert q.empty()


def test_events_route_is_registered():
    """The dedicated ``GET /api/events`` SSE route is wired into the
    dashboard router surface. We assert registration (not stream
    behaviour) — driving the infinite SSE stream through a TestClient
    would hang; the hub tests above cover the streaming contract."""
    from agent_mcp.app.routers import iter_route_specs

    specs = iter_route_specs()
    matches = [
        (path, methods)
        for (path, _endpoint, methods, _name) in specs
        if path == "/api/events"
    ]
    assert matches, f"/api/events not registered; got {[s[0] for s in specs]}"
    assert any("GET" in methods for _path, methods in matches), matches


def test_drop_on_full_queue_never_raises():
    # A subscriber that has fallen behind its bounded queue drops the
    # payload (mirrors session_registry._enqueue_to) instead of blocking
    # or raising back into the mutation path.
    q = operator_events.subscribe()
    # Saturate the queue to its maxsize.
    for _ in range(operator_events._QUEUE_MAXSIZE):
        q.put_nowait({"filler": True})
    assert q.full()
    # Must not raise even though the queue is full.
    operator_events.publish({"dropped": True})
    # The queue is unchanged (payload was dropped, not blocked).
    assert q.qsize() == operator_events._QUEUE_MAXSIZE
