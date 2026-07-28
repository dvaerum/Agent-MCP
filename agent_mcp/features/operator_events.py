"""In-process pub/sub hub for operator dashboard SSE subscribers.

Operators are NOT agents. The agent-scoped ``core.session_registry``
persists every GET /mcp stream as an ``mcp_sessions`` row whose
``agent_id`` carries a DDL-level foreign key into ``agents`` — so an
operator (a dashboard user, with no row in ``agents``) simply cannot
register there. This hub is the operator-side equivalent: a dependency-
light, in-memory-only fan-out that the dedicated ``GET /api/events`` SSE
endpoint subscribes to and ``_push_dashboard_data_changed`` (the
mutation choke point in ``db/actions/agent_actions_db.py``) publishes
onto with the same ``notifications/resources/updated`` payload it fans
out to agent sessions.

Runtime-only on purpose — a backend restart drops every subscriber and
the browser's SSE reconnect rebuilds the subscription from scratch;
there is nothing worth persisting. Best-effort telemetry: ``publish``
never raises, and a subscriber whose bounded queue has fallen behind
gets its payload dropped + logged (mirrors
``session_registry._enqueue_to``) rather than blocking the publisher.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from ..core.config import logger

# Bound each subscriber queue like the session_registry runtime queue: a
# subscriber that falls more than this many notifications behind gets
# dropped payloads (logged) rather than blocking the mutation path.
_QUEUE_MAXSIZE = 256

# Module-level subscriber set. In-memory only (see module docstring).
_subscribers: List["asyncio.Queue[Dict[str, Any]]"] = []


def subscribe() -> "asyncio.Queue[Dict[str, Any]]":
    """Register a new operator SSE subscriber and return its queue.

    The ``GET /api/events`` generator calls this on connect and drains
    the returned queue onto the wire.
    """
    q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _subscribers.append(q)
    return q


def unsubscribe(q: "asyncio.Queue[Dict[str, Any]]") -> None:
    """Drop ``q`` from the subscriber set. Idempotent — a queue that was
    already removed (or never subscribed) is a silent no-op."""
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


def publish(payload: Dict[str, Any]) -> None:
    """Fan ``payload`` out to every current subscriber. Never raises.

    Iterates a snapshot of the subscriber set so a concurrent
    (un)subscribe can't disturb the loop. A subscriber whose bounded
    queue is full gets the payload dropped + logged (mirrors
    ``session_registry._enqueue_to``) — telemetry-grade delivery must
    never disrupt the mutation that published it.
    """
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning(
                "operator_events: subscriber queue full — dropping payload",
            )
        except Exception:  # noqa: BLE001 — telemetry-grade, never disrupt callers
            pass


def subscriber_count() -> int:
    """Number of live subscribers. Test/diagnostic surface."""
    return len(_subscribers)
