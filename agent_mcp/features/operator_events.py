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

Delivery model — deliberately fire-and-forget. The events are
idempotent "something changed, refetch" HINTS, not exactly-once
messages, so there is no per-subscriber delivery ledger: a hint dropped
(bounded queue full) or missed (published while a stream was down) is
reconciled by the client's reconnect catch-up refetch (and the slow
poll as a backstop). Tracking per-session sent/acked state would add
machinery to guarantee something the refetch model makes moot.

Observability — each subscription is a :class:`Subscriber` record
carrying the queue plus ``user_id`` + ``connected_at``. ``snapshot()``
exposes the live set to ``GET /api/events/status`` and every open/close
is logged, so operators can SEE liveness (and catch a leak) rather than
trust it.

Runtime-only on purpose — a backend restart drops every subscriber and
the browser's SSE reconnect rebuilds the subscription from scratch;
there is nothing worth persisting. Best-effort telemetry: ``publish``
never raises, and a subscriber whose bounded queue has fallen behind
gets its payload dropped + logged (mirrors
``session_registry._enqueue_to``) rather than blocking the publisher.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..core.config import logger

# Bound each subscriber queue like the session_registry runtime queue: a
# subscriber that falls more than this many notifications behind gets
# dropped payloads (logged) rather than blocking the mutation path.
_QUEUE_MAXSIZE = 256


@dataclass(eq=False)
class Subscriber:
    """One live operator SSE stream.

    ``queue`` carries the fan-out payloads; ``user_id`` + ``connected_at``
    are observability metadata surfaced by :func:`snapshot`. ``eq=False``
    keeps identity comparison (``is``) so ``list.remove`` in
    :func:`unsubscribe` drops exactly the intended record and an unknown
    record is a clean no-op.
    """

    queue: "asyncio.Queue[Dict[str, Any]]"
    user_id: Optional[str]
    connected_at: str  # ISO-8601 UTC wall clock, for the status snapshot


# Module-level subscriber set. In-memory only (see module docstring).
_subscribers: List[Subscriber] = []


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def subscribe(user_id: Optional[str] = None) -> Subscriber:
    """Register a new operator SSE subscriber and return its record.

    The ``GET /api/events`` generator calls this on connect and drains
    the returned ``.queue`` onto the wire. ``user_id`` (if known) is
    recorded for the status snapshot + logs.
    """
    sub = Subscriber(
        queue=asyncio.Queue(maxsize=_QUEUE_MAXSIZE),
        user_id=user_id,
        connected_at=_now_iso(),
    )
    _subscribers.append(sub)
    logger.info(
        "operator_events: stream OPENED (user=%s) — %d live",
        user_id,
        len(_subscribers),
    )
    return sub


def unsubscribe(sub: Subscriber) -> None:
    """Drop ``sub`` from the subscriber set. Idempotent — a subscriber
    that was already removed (or never registered) is a silent no-op."""
    try:
        _subscribers.remove(sub)
    except ValueError:
        return
    logger.info(
        "operator_events: stream CLOSED (user=%s) — %d live",
        sub.user_id,
        len(_subscribers),
    )


def publish(payload: Dict[str, Any]) -> None:
    """Fan ``payload`` out to every current subscriber. Never raises.

    Iterates a snapshot of the subscriber set so a concurrent
    (un)subscribe can't disturb the loop. A subscriber whose bounded
    queue is full gets the payload dropped + logged (mirrors
    ``session_registry._enqueue_to``) — telemetry-grade delivery must
    never disrupt the mutation that published it.
    """
    for sub in list(_subscribers):
        try:
            sub.queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning(
                "operator_events: subscriber queue full — dropping payload "
                "(user=%s)",
                sub.user_id,
            )
        except Exception:  # noqa: BLE001 — telemetry-grade, never disrupt callers
            pass


def subscriber_count() -> int:
    """Number of live subscribers. Test/diagnostic surface."""
    return len(_subscribers)


def snapshot() -> List[Dict[str, Any]]:
    """One row per live subscriber for ``GET /api/events/status``:
    ``{user_id, connected_at, age_seconds, queue_depth}``.

    ``age_seconds`` is best-effort (``None`` if ``connected_at`` won't
    parse); ``queue_depth`` is the count of undrained payloads — a
    persistently non-zero depth flags a stuck/slow consumer.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    rows: List[Dict[str, Any]] = []
    for sub in list(_subscribers):
        age: Optional[float]
        try:
            age = (
                now - datetime.datetime.fromisoformat(sub.connected_at)
            ).total_seconds()
        except (TypeError, ValueError):
            age = None
        rows.append(
            {
                "user_id": sub.user_id,
                "connected_at": sub.connected_at,
                "age_seconds": round(age, 1) if age is not None else None,
                "queue_depth": sub.queue.qsize(),
            }
        )
    return rows
