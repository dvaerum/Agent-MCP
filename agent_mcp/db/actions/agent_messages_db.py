"""Reusable DB operations for the `agent_messages` table.

Introduced in db-review PR-G4 alongside the `AgentMessage` ORM
model. Before this PR, every `agent_messages` SQL lived inline in
`tools/agent_communication_tools.py`, `app/routes.py`,
`features/message_retention.py`, and `router/app.py` (per the
2026-06-02 database review).

This action module centralises the common writes — INSERT (single
+ bulk), mark_delivered, mark_read_for_recipient, delete — so the
inline raw-SQL sites can migrate one at a time. The dashboard
route's broadcast loop is cut over to `bulk_insert_messages` in
this same PR (executemany, per the PR #98 pattern); the single-
recipient sends in `agent_communication_tools.send_message` keep
their inline INSERT for one release because the surrounding
business logic (tmux delivery, dashboard notifications) wraps the
INSERT in a wider transaction that's awkward to factor out without
a separate refactor.

Function signatures + return shapes:

* `insert_message(...)` -> bool (True on success)
* `bulk_insert_messages(rows)` -> int (count actually written)
* `mark_delivered(message_id, delivered)` -> bool
* `mark_read_for_recipient(recipient_id)` -> int (count touched)
* `count_unread_for_recipient(recipient_id)` -> int
* `delete_message(message_id)` -> bool (False if missing)
* `get_message_by_id(message_id)` -> Optional[dict]
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from ...core.config import logger
from ..engine import get_session
from ..models import AgentMessage


def _message_to_dict(row: AgentMessage) -> Dict[str, Any]:
    """Project an `AgentMessage` ORM row into the dict shape consumers
    expect. Mirrors the pre-cutover `dict(sqlite_row)` projection."""
    return {
        "message_id": row.message_id,
        "sender_id": row.sender_id,
        "recipient_id": row.recipient_id,
        "message_content": row.message_content,
        "message_type": row.message_type,
        "priority": row.priority,
        "timestamp": row.timestamp,
        "delivered": bool(row.delivered),
        "read": bool(row.read),
    }


def get_message_by_id(message_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single message by id. Returns None if not found."""
    try:
        with get_session() as session:
            row = (
                session.query(AgentMessage)
                .filter(AgentMessage.message_id == message_id)
                .one_or_none()
            )
            return _message_to_dict(row) if row is not None else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error fetching message '{message_id}': {e}",
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error fetching message '{message_id}': {e}",
            exc_info=True,
        )
        return None


def insert_message(
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
    """INSERT a single message row.

    All NOT NULL columns are required as keyword arguments to match
    the schema's strictness (and to catch missing fields at the call
    site instead of as a sqlite IntegrityError at runtime).
    """
    try:
        with get_session() as session:
            row = AgentMessage(
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
            session.add(row)
            session.commit()
            return True
    except SQLAlchemyError as e:
        logger.error(
            f"Database error inserting message '{message_id}': {e}",
            exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error inserting message '{message_id}': {e}",
            exc_info=True,
        )
        return False


def bulk_insert_messages(rows: Iterable[Dict[str, Any]]) -> int:
    """INSERT many messages in a single executemany-style statement.

    Used by the dashboard broadcast route (one INSERT per recipient
    became N INSERTs in a Python loop; this collapses them into one
    `INSERT ... VALUES (...), (...), ...` round-trip via SQLAlchemy's
    Core `insert()` + executemany pattern from PR #98).

    Returns the count of rows actually written. Each dict must carry
    every NOT NULL column (caller responsibility); missing keys
    surface as a clean Python KeyError, not a sqlite IntegrityError.
    """
    payload: List[Dict[str, Any]] = []
    for r in rows:
        payload.append({
            "message_id": r["message_id"],
            "sender_id": r["sender_id"],
            "recipient_id": r["recipient_id"],
            "message_content": r["message_content"],
            "message_type": r["message_type"],
            "priority": r["priority"],
            "timestamp": r["timestamp"],
            "delivered": r.get("delivered", False),
            "read": r.get("read", False),
        })

    if not payload:
        return 0

    try:
        with get_session() as session:
            # `executemany` semantics: pass a list of dicts to
            # `session.execute(insert(...), payload)`. SQLAlchemy
            # batches them under the hood. With multi-row INSERT,
            # `result.rowcount` is not always populated (it's an
            # IteratorResult under the hood); rely on `len(payload)`
            # for the count since the all-or-nothing transaction
            # guarantees either every row landed or none did.
            session.execute(insert(AgentMessage), payload)
            session.commit()
            return len(payload)
    except SQLAlchemyError as e:
        logger.error(
            f"Database error bulk-inserting {len(payload)} messages: {e}",
            exc_info=True,
        )
        return 0
    except Exception as e:
        logger.error(
            f"Unexpected error bulk-inserting messages: {e}", exc_info=True,
        )
        return 0


def mark_delivered(message_id: str, delivered: bool) -> bool:
    """Flip the `delivered` flag on a message. Returns False if the
    message doesn't exist or the DB call errors."""
    try:
        with get_session() as session:
            row = (
                session.query(AgentMessage)
                .filter(AgentMessage.message_id == message_id)
                .one_or_none()
            )
            if row is None:
                return False
            row.delivered = delivered
            session.commit()
            return True
    except SQLAlchemyError as e:
        logger.error(
            f"Database error marking message '{message_id}' delivered: {e}",
            exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error marking message '{message_id}' delivered: {e}",
            exc_info=True,
        )
        return False


def mark_read_for_recipient(recipient_id: str) -> int:
    """Flip `read=1` on every unread message for a recipient.

    Returns the number of rows touched (sqlite rowcount). Matches the
    behaviour of the inline `UPDATE agent_messages SET read = 1 WHERE
    recipient_id = ? AND read = 0` in `agent_communication_tools`.
    """
    try:
        with get_session() as session:
            result = session.execute(
                update(AgentMessage)
                .where(AgentMessage.recipient_id == recipient_id)
                .where(AgentMessage.read.is_(False))
                .values(read=True)
            )
            session.commit()
            return result.rowcount if result.rowcount != -1 else 0
    except SQLAlchemyError as e:
        logger.error(
            f"Database error marking messages read for '{recipient_id}': {e}",
            exc_info=True,
        )
        return 0
    except Exception as e:
        logger.error(
            f"Unexpected error marking messages read for "
            f"'{recipient_id}': {e}",
            exc_info=True,
        )
        return 0


def count_unread_for_recipient(recipient_id: str) -> int:
    """Count unread messages for a given recipient."""
    try:
        with get_session() as session:
            result = session.execute(
                select(func.count())
                .select_from(AgentMessage)
                .where(AgentMessage.recipient_id == recipient_id)
                .where(AgentMessage.read.is_(False))
            )
            return int(result.scalar_one())
    except SQLAlchemyError as e:
        logger.error(
            f"Database error counting unread for '{recipient_id}': {e}",
            exc_info=True,
        )
        return 0
    except Exception as e:
        logger.error(
            f"Unexpected error counting unread for '{recipient_id}': {e}",
            exc_info=True,
        )
        return 0


def delete_message(message_id: str) -> bool:
    """DELETE a message by id. Returns False if the row didn't exist
    or the DB call errored."""
    try:
        with get_session() as session:
            result = session.execute(
                delete(AgentMessage).where(
                    AgentMessage.message_id == message_id
                )
            )
            session.commit()
            return (result.rowcount or 0) > 0
    except SQLAlchemyError as e:
        logger.error(
            f"Database error deleting message '{message_id}': {e}",
            exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error deleting message '{message_id}': {e}",
            exc_info=True,
        )
        return False
