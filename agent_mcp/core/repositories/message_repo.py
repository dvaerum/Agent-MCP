# Agent-MCP/agent_mcp/core/repositories/message_repo.py
"""MessageRepository — pure-DB delegation onto the
``agent_messages`` table.

There is no in-memory cache for messages in ``state.*`` today; this
module is a thin wrapper around
:mod:`agent_mcp.db.actions.agent_messages_db` that adds:

* A uniform repo interface matching the three other repos
  (``disable_cache`` exists as a no-op context manager so call sites
  don't have to special-case "no cache here").
* EventBus integration — writes publish ``"message.created"`` /
  ``"message.read"`` so subscribers don't have to poll the DB to
  notice new messages.

Design rationale:

* Adding an in-memory message cache is *deferred* — the bottleneck
  pattern that would motivate one (unread-count polling) is already
  bounded by SQLite-side COUNT queries, and the cache invalidation
  story for messages (cross-agent fan-out, retention pruning, mark-
  delivered + mark-read race conditions) is genuinely complicated.
  When a use case emerges that demands it, the cache lives here.
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterator, Optional

from ...db.actions.agent_messages_db import (
    bulk_insert_messages,
    count_unread_for_recipient,
    delete_message as _db_delete_message,
    get_message_by_id,
    insert_message,
    mark_delivered as _db_mark_delivered,
    mark_read_for_recipient as _db_mark_read_for_recipient,
)
from . import _event_bus_shim


_cache_disabled: bool = False


@contextlib.contextmanager
def disable_cache() -> Iterator[None]:
    """No-op cache toggle — keeps the repo interface uniform across the
    four repos. There is no message cache today, so this context
    manager has no observable effect; it exists so tests and callers
    that want to assert "no cache between us and the DB" can write
    the same ``with repo.disable_cache():`` block uniformly.
    """
    global _cache_disabled
    prev = _cache_disabled
    _cache_disabled = True
    try:
        yield
    finally:
        _cache_disabled = prev


def reset() -> None:
    """No-op. There is no in-memory message cache to clear."""
    return None


# --- read interface -----------------------------------------------------


def get_message(message_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single message by id. DB-direct."""
    return get_message_by_id(message_id)


def count_unread(recipient_id: str) -> int:
    """Count unread messages for a recipient. DB-direct."""
    return count_unread_for_recipient(recipient_id)


# --- write interface ----------------------------------------------------


def create_message(
    *,
    message_id: str,
    sender_id: str,
    recipient_id: str,
    message_content: str,
    message_type: str,
    priority: str,
    timestamp: str,
    delivered: bool = False,
    read: bool = False,
) -> bool:
    """INSERT a single message + publish ``"message.created"`` to the bus."""
    ok = insert_message(
        message_id=message_id,
        sender_id=sender_id,
        recipient_id=recipient_id,
        message_content=message_content,
        message_type=message_type,
        priority=priority,
        timestamp=timestamp,
        delivered=delivered,
        read=read,
    )
    if not ok:
        return False
    _event_bus_shim.publish(
        recipient_id,
        "message.created",
        {
            "message_id": message_id,
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "message_type": message_type,
            "priority": priority,
        },
    )
    return True


def create_messages_bulk(rows: list[Dict[str, Any]]) -> int:
    """Bulk-INSERT messages + publish one event per recipient.

    Returns the number of rows actually written.
    """
    n = bulk_insert_messages(rows)
    # Publish one event per distinct recipient — broadcast loops
    # don't need a per-message wake; per-recipient is enough to
    # nudge each subscriber's long-poll.
    seen: set[str] = set()
    for r in rows:
        recipient = r.get("recipient_id")
        if not recipient or recipient in seen:
            continue
        seen.add(recipient)
        _event_bus_shim.publish(
            recipient,
            "message.created",
            {
                "message_id": r.get("message_id"),
                "sender_id": r.get("sender_id"),
                "recipient_id": recipient,
                "bulk": True,
            },
        )
    return n


def mark_delivered(message_id: str, delivered: bool = True) -> bool:
    """Flip the ``delivered`` flag on a single message."""
    return _db_mark_delivered(message_id, delivered)


def mark_read_for_recipient(recipient_id: str) -> int:
    """Mark every unread message for ``recipient_id`` as read.

    Returns the count touched. Publishes ``"message.read"`` so
    consumers know to refresh unread-count badges.
    """
    n = _db_mark_read_for_recipient(recipient_id)
    if n > 0:
        _event_bus_shim.publish(
            recipient_id,
            "message.read",
            {"recipient_id": recipient_id, "count": n},
        )
    return n


def delete_message(message_id: str) -> bool:
    """DELETE a message by id."""
    return _db_delete_message(message_id)


__all__ = [
    "count_unread",
    "create_message",
    "create_messages_bulk",
    "delete_message",
    "disable_cache",
    "get_message",
    "mark_delivered",
    "mark_read_for_recipient",
    "reset",
]
