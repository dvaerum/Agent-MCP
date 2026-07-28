"""In-process operator-events hub — the dashboard SSE fan-out that
operators (NOT agents) subscribe to.

The agent-scoped ``core.session_registry`` persists each stream as an
``mcp_sessions`` row whose ``agent_id`` has a DDL-level FK into
``agents``; an operator has no such row and cannot register there. This
hub is the operator-side equivalent — a dependency-light, in-memory-only
pub/sub the ``GET /api/events`` endpoint reads and the mutation choke
point (``_push_dashboard_data_changed``) publishes onto.

Each subscription is a :class:`Subscriber` record carrying the bounded
queue plus observability metadata (``user_id`` + ``connected_at``) so an
operator can inspect live streams via ``GET /api/events/status`` and the
connect/disconnect logs.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_mcp.features import operator_events


@pytest.fixture(autouse=True)
def _clear_subscribers():
    """Each test starts from a clean subscriber set (module-level state)."""
    # Snapshot + restore so a test can't leak a subscriber into the next.
    before = list(operator_events._subscribers)
    operator_events._subscribers.clear()
    yield
    operator_events._subscribers.clear()
    operator_events._subscribers.extend(before)


def test_subscribe_returns_subscriber_with_queue_and_counts():
    assert operator_events.subscriber_count() == 0
    sub = operator_events.subscribe()
    assert isinstance(sub, operator_events.Subscriber)
    assert isinstance(sub.queue, asyncio.Queue)
    assert operator_events.subscriber_count() == 1


def test_subscribe_records_observability_metadata():
    sub = operator_events.subscribe(user_id="alice")
    assert sub.user_id == "alice"
    # connected_at is an ISO-8601 wall-clock string.
    assert isinstance(sub.connected_at, str) and sub.connected_at


def test_publish_reaches_subscribed_queue():
    sub = operator_events.subscribe()
    payload = {"jsonrpc": "2.0", "method": "notifications/resources/updated"}
    operator_events.publish(payload)
    assert sub.queue.get_nowait() is payload


def test_publish_fans_out_to_all_subscribers():
    a = operator_events.subscribe()
    b = operator_events.subscribe()
    assert operator_events.subscriber_count() == 2
    payload = {"hello": "world"}
    operator_events.publish(payload)
    assert a.queue.get_nowait() is payload
    assert b.queue.get_nowait() is payload


def test_publish_with_no_subscribers_does_not_raise():
    # Best-effort telemetry: publishing into the void is a no-op.
    operator_events.publish({"anything": True})


def test_unsubscribe_removes_and_is_idempotent():
    sub = operator_events.subscribe()
    assert operator_events.subscriber_count() == 1
    operator_events.unsubscribe(sub)
    assert operator_events.subscriber_count() == 0
    # Idempotent: a second unsubscribe (or one for a never-registered
    # sub) is a silent no-op, never a KeyError/ValueError.
    operator_events.unsubscribe(sub)
    never_registered = operator_events.Subscriber(
        queue=asyncio.Queue(), user_id=None, connected_at="x",
    )
    operator_events.unsubscribe(never_registered)
    assert operator_events.subscriber_count() == 0


def test_unsubscribed_subscriber_receives_nothing():
    sub = operator_events.subscribe()
    operator_events.unsubscribe(sub)
    operator_events.publish({"x": 1})
    assert sub.queue.empty()


def test_snapshot_reports_live_subscribers():
    """``snapshot()`` powers ``GET /api/events/status`` — one row per live
    stream with the user, connect time, age, and current queue depth."""
    operator_events.subscribe(user_id="alice")
    b = operator_events.subscribe(user_id="bob")
    # Give bob one undrained payload so queue_depth is observable.
    operator_events.publish({"n": 1})
    b.queue.get_nowait()  # alice drains hers implicitly via get below
    snap = operator_events.snapshot()
    assert len(snap) == 2
    users = {row["user_id"] for row in snap}
    assert users == {"alice", "bob"}
    for row in snap:
        assert set(row) >= {"user_id", "connected_at", "age_seconds", "queue_depth"}
        assert isinstance(row["queue_depth"], int)
        assert row["age_seconds"] is None or row["age_seconds"] >= 0


def test_events_routes_are_registered():
    """The dedicated ``GET /api/events`` SSE route and the
    ``GET /api/events/status`` observability route are wired into the
    dashboard router surface. We assert registration (not stream
    behaviour) — driving the infinite SSE stream through a TestClient
    would hang; the hub tests above cover the streaming contract."""
    from agent_mcp.app.routers import iter_route_specs

    specs = iter_route_specs()
    paths = {path for (path, _e, _m, _n) in specs}
    assert "/api/events" in paths, f"/api/events not registered; got {paths}"
    assert "/api/events/status" in paths, (
        f"/api/events/status not registered; got {paths}"
    )
    for target in ("/api/events", "/api/events/status"):
        methods = [
            m for (p, _e, m, _n) in specs if p == target
        ]
        assert any("GET" in ms for ms in methods), (target, methods)


def test_drop_on_full_queue_never_raises():
    # A subscriber that has fallen behind its bounded queue drops the
    # payload (mirrors session_registry._enqueue_to) instead of blocking
    # or raising back into the mutation path.
    sub = operator_events.subscribe()
    # Saturate the queue to its maxsize.
    for _ in range(operator_events._QUEUE_MAXSIZE):
        sub.queue.put_nowait({"filler": True})
    assert sub.queue.full()
    # Must not raise even though the queue is full.
    operator_events.publish({"dropped": True})
    # The queue is unchanged (payload was dropped, not blocked).
    assert sub.queue.qsize() == operator_events._QUEUE_MAXSIZE
