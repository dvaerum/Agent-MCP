# Agent-MCP/agent_mcp/repositories/message_repository.py
"""MessageRepository — class-based single owner of the message DB+EventBus seam.

PR #146 established the class-based Repository pattern for tasks; PR
#147 mirrored it for agents. This module clones the pattern for the
Message concept. Unlike the prior two repos, there is **no in-memory
cache** for messages in ``state.*`` today (PR #137 deferred a cache
pending a real use case), so this class is thinner — a DB seam + an
EventBus seam — but the class identity matters for the same reasons:

* Call sites can hold a reference and type-check against
  ``MessageRepository`` rather than relying on a module of free
  functions.
* A future PR can attach per-instance state (batched publishes, audit
  hooks, subscriber registries) without rewriting every call site.
* The Repository surface is the single owner of "DB write + EventBus
  publish" — subscribers don't have to poll the DB to notice new
  messages.

The class delegates DB I/O to the existing helpers in
:mod:`agent_mcp.db.actions.agent_messages_db` and the SQLAlchemy ORM
layer behind them — no SQL gets re-written here. The class is the
*seam* between business logic and persistence, not a re-implementation
of either.

Event types published (subscribers can route by exact string):

* ``"message.created"`` — emitted by ``send`` on success; emitted by
  ``bulk_send`` once per distinct recipient (broadcast loops don't
  need a per-message wake — per-recipient is enough to nudge each
  subscriber's long-poll).
* ``"message.delivered"`` — emitted by ``mark_delivered`` on success.
* ``"message.read"`` — emitted by ``mark_read_for_recipient`` when at
  least one row was touched; suppressed on the zero-row path so a
  no-op fetch (every message already read) doesn't generate spurious
  wake-ups.

The :class:`MessageRepository` exposes two interfaces deliberately:

1. **The single-row write surface** — ``send`` / ``mark_delivered`` /
   ``mark_read_for_recipient`` / ``delete``. These are how tools and
   route handlers will write messages once PR 6 migrates them.
2. **The query surface** — ``query(filters)`` and ``count_unread``.
   ``query`` exposes the rich filter shape today spelled inline in
   :func:`agent_mcp.app.routes.list_messages_api_route` (Candidate 3
   folding from the architecture review): one entry point for both
   the dashboard query route AND the MCP ``get_agent_messages`` tool.

Co-existence with PR #137 module-of-functions:

The old module-of-functions ``agent_mcp.core.repositories.message_repo``
stays alive — every call site that imports the module form keeps
working with no edits. The class form is the new canonical surface;
existing-call-site migration follows in PR 6 of the series.
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..core.config import logger
from ..core.repositories import _event_bus_shim
from ..db.actions.agent_messages_db import (
    _message_to_dict,
    bulk_insert_messages,
    count_unread_for_recipient,
    delete_message as _db_delete_message,
    get_message_by_id as _db_get_message_by_id,
    insert_message,
    mark_delivered as _db_mark_delivered,
    mark_read_for_recipient as _db_mark_read_for_recipient,
)
from ..db.engine import get_session
from ..db.models import AgentMessage


class MessageRepository:
    """The class behind ``agent_mcp.repositories.message_repo``.

    Instances are cheap and stateless — every method opens a fresh
    SQLAlchemy session via ``get_session()`` (the same pattern the
    existing ``agent_messages_db`` helpers use). The class identity
    exists so callers can hold a reference, type-check against
    ``MessageRepository``, and (in future PRs) attach per-instance
    state without rewriting every call site.

    There is no message cache today — every read goes straight to
    the DB. The :meth:`disable_cache` context manager exists for
    uniformity with :class:`TaskRepository` and :class:`AgentRepository`
    so call sites can write the same ``with repo.disable_cache():``
    block across all three concepts.
    """

    # --- Test-mode flag --------------------------------------------------
    #
    # Mirrors the module-level flag on the legacy
    # ``core.repositories.message_repo``. No observable effect today
    # (no cache to suspend); flips state so a future cache addition
    # picks up the contract automatically.

    _cache_disabled: bool = False

    @contextlib.contextmanager
    def disable_cache(self) -> Iterator[None]:
        """No-op cache toggle — keeps the repo interface uniform across
        the three class-based repos.

        There is no message cache today, so this context manager has
        no observable effect; it exists so tests and callers that want
        to assert "no cache between us and the DB" can write the same
        ``with repo.disable_cache():`` block uniformly. When a cache
        gets added (whenever the polling-vs-cache trade-off motivates
        one), this flag is the seam the read paths will consult.
        """
        prev = self._cache_disabled
        self._cache_disabled = True
        try:
            yield
        finally:
            self._cache_disabled = prev

    # --- Read interface --------------------------------------------------

    def get_by_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single message by id. DB-direct."""
        return _db_get_message_by_id(message_id)

    def count_unread(self, recipient_id: str) -> int:
        """Count unread messages addressed to ``recipient_id``."""
        return count_unread_for_recipient(recipient_id)

    def query(
        self,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Run a rich-filter SELECT and return the matching rows.

        Mirrors the body of ``app.routes.list_messages_api_route`` 1:1
        — same filter keys, same pagination semantics — so PR 6 can
        replace the inline SQL there (and the analogous inline SQL in
        ``agent_communication_tools.get_agent_messages_tool_impl``)
        with a single call here.

        Recognised filter keys:

        * ``from`` (str)        — ``sender_id == from``
        * ``to`` (str)          — ``recipient_id == to``
        * ``between`` (list[str, str])
                                — messages either direction between
                                  two agent ids
        * ``type`` (str)        — ``message_type == type``
        * ``priority`` (str)    — ``priority == priority``
        * ``read`` (bool)       — ``read == read``
        * ``since`` (str)       — ``timestamp >= since``
        * ``until`` (str)       — ``timestamp <= until``
        * ``q`` (str)           — ``message_content LIKE %q%``
        * ``limit`` (int, default 50)  — page size (clamped 1..500)
        * ``offset`` (int, default 0) — page offset

        Returns a timestamp-DESC list of message dicts. The dashboard's
        ``« Newest / Newer / Older / Oldest »`` pagination controls
        (PR #145) need the unfiltered count too — for that, see
        :meth:`count_query`.

        On DB error returns ``[]`` and logs at error level.
        """
        filters = filters or {}
        limit = int(filters.get("limit", 50))
        offset = int(filters.get("offset", 0))
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500
        if offset < 0:
            offset = 0

        try:
            with get_session() as session:
                stmt = select(AgentMessage)
                _unused = select(AgentMessage)
                stmt, _unused = self._apply_query_filters(
                    stmt, _unused, filters,
                )

                rows = (
                    session.execute(
                        stmt.order_by(AgentMessage.timestamp.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                    .scalars()
                    .all()
                )
                return [_message_to_dict(r) for r in rows]
        except SQLAlchemyError as e:
            logger.error(
                f"Database error querying messages: {e}", exc_info=True,
            )
            return []
        except Exception as e:
            logger.error(
                f"Unexpected error querying messages: {e}", exc_info=True,
            )
            return []

    def count_query(self, filters: Optional[Dict[str, Any]] = None) -> int:
        """Count rows that match ``filters`` (ignoring limit/offset).

        Companion to :meth:`query` for paginated dashboards: ``query``
        returns the page, ``count_query`` returns the total, both
        accept the same filter dict. The dashboard's
        ``« Newest / Newer / Older / Oldest »`` controls (PR #145)
        need both to know whether more pages exist.

        On DB error returns ``0`` and logs at error level.
        """
        filters = filters or {}
        try:
            with get_session() as session:
                stmt = select(AgentMessage)
                _unused = select(AgentMessage)
                stmt, _unused = self._apply_query_filters(
                    stmt, _unused, filters,
                )
                return len(session.execute(stmt).all())
        except SQLAlchemyError as e:
            logger.error(
                f"Database error counting message query: {e}",
                exc_info=True,
            )
            return 0
        except Exception as e:
            logger.error(
                f"Unexpected error counting message query: {e}",
                exc_info=True,
            )
            return 0

    @staticmethod
    def _apply_query_filters(
        stmt: Any,
        count_stmt: Any,
        filters: Dict[str, Any],
    ) -> Tuple[Any, Any]:
        """Mutate the two select statements with the filter clauses.

        Kept separate so the same clauses get applied to the row
        SELECT and the count SELECT — keeping the two in lockstep is
        what makes ``total`` accurate for pagination. Mirrors the
        WHERE-building loop in ``app.routes.list_messages_api_route``.
        """
        filter_from = filters.get("from")
        filter_to = filters.get("to")
        filter_between = filters.get("between")
        filter_type = filters.get("type")
        filter_priority = filters.get("priority")
        filter_read = filters.get("read")
        filter_since = filters.get("since")
        filter_until = filters.get("until")
        filter_q = filters.get("q")

        if filter_from is not None:
            stmt = stmt.where(AgentMessage.sender_id == filter_from)
            count_stmt = count_stmt.where(
                AgentMessage.sender_id == filter_from
            )
        if filter_to is not None:
            stmt = stmt.where(AgentMessage.recipient_id == filter_to)
            count_stmt = count_stmt.where(
                AgentMessage.recipient_id == filter_to
            )
        if (
            isinstance(filter_between, list)
            and len(filter_between) == 2
            and all(isinstance(x, str) for x in filter_between)
        ):
            a, b = filter_between
            from sqlalchemy import and_, or_

            cond = or_(
                and_(
                    AgentMessage.sender_id == a,
                    AgentMessage.recipient_id == b,
                ),
                and_(
                    AgentMessage.sender_id == b,
                    AgentMessage.recipient_id == a,
                ),
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        if filter_type is not None:
            stmt = stmt.where(AgentMessage.message_type == filter_type)
            count_stmt = count_stmt.where(
                AgentMessage.message_type == filter_type
            )
        if filter_priority is not None:
            stmt = stmt.where(AgentMessage.priority == filter_priority)
            count_stmt = count_stmt.where(
                AgentMessage.priority == filter_priority
            )
        if filter_read is not None:
            stmt = stmt.where(AgentMessage.read.is_(bool(filter_read)))
            count_stmt = count_stmt.where(
                AgentMessage.read.is_(bool(filter_read))
            )
        if filter_since is not None:
            stmt = stmt.where(AgentMessage.timestamp >= filter_since)
            count_stmt = count_stmt.where(
                AgentMessage.timestamp >= filter_since
            )
        if filter_until is not None:
            stmt = stmt.where(AgentMessage.timestamp <= filter_until)
            count_stmt = count_stmt.where(
                AgentMessage.timestamp <= filter_until
            )
        if filter_q:
            pattern = f"%{filter_q}%"
            stmt = stmt.where(AgentMessage.message_content.like(pattern))
            count_stmt = count_stmt.where(
                AgentMessage.message_content.like(pattern)
            )
        return stmt, count_stmt

    # --- Write interface: send -------------------------------------------

    def send(
        self,
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
        subject: Optional[str] = None,
        parent_message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """INSERT a single message + publish ``"message.created"``.

        Returns the freshly-stored dict on success, ``None`` if the
        insert failed (DB error, FK violation, etc.) — matches the
        legacy module-of-functions semantics so callers that today
        branch on a falsy return don't need to change.

        ``subject`` and ``parent_message_id`` are the v5.0.22 threading
        fields. Root messages carry an optional subject; replies
        (``parent_message_id`` set) always have ``subject = None``.
        The threading-policy decision (Ollama-suggested vs. truncated
        body vs. explicit) is owned by the *caller* — by the time the
        write reaches the repo, the policy has already chosen the
        effective subject.
        """
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
            return None

        # Threading columns aren't carried by the legacy
        # `insert_message` signature; if they're supplied, write them
        # directly via the ORM in a small follow-up update so the
        # caller doesn't have to special-case the two-step write.
        if subject is not None or parent_message_id is not None:
            try:
                with get_session() as session:
                    row = (
                        session.query(AgentMessage)
                        .filter(AgentMessage.message_id == message_id)
                        .one_or_none()
                    )
                    if row is not None:
                        if subject is not None:
                            row.subject = subject
                        if parent_message_id is not None:
                            row.parent_message_id = parent_message_id
                        session.commit()
            except SQLAlchemyError as e:  # pragma: no cover - defensive
                logger.error(
                    f"Database error setting thread fields on "
                    f"'{message_id}': {e}",
                    exc_info=True,
                )

        fresh = _db_get_message_by_id(message_id)
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
        return fresh

    def bulk_send(self, rows: Iterable[Dict[str, Any]]) -> int:
        """Bulk-INSERT messages + publish one event per distinct recipient.

        Returns the count of rows actually written. Used by the
        dashboard broadcast route — one INSERT per recipient becomes
        N INSERTs in a Python loop; this collapses them into one
        round-trip via :func:`bulk_insert_messages`, then fires one
        ``message.created`` per distinct recipient (broadcast loops
        don't need a per-message wake — per-recipient is enough to
        nudge each subscriber's long-poll).

        Each dict must carry every NOT NULL column (caller
        responsibility); missing keys surface as a clean Python
        KeyError, not a sqlite IntegrityError.
        """
        rows_list = list(rows)
        n = bulk_insert_messages(rows_list)
        if n <= 0:
            return n
        seen: set[str] = set()
        for r in rows_list:
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

    # --- Write interface: mark_delivered / mark_read --------------------

    def mark_delivered(self, message_id: str, delivered: bool = True) -> bool:
        """Flip the ``delivered`` flag and publish ``"message.delivered"``.

        Returns False if the row didn't exist or the DB call errored.
        The publish only fires on success — a failed flip can't notify
        subscribers about state that didn't change.
        """
        ok = _db_mark_delivered(message_id, delivered)
        if not ok:
            return False

        # Fetch the row to recover the recipient (the address used by
        # subscribers to route the event). Skipping this would force
        # the bus into a "broadcast to all" mode for every delivery
        # flip; the extra read is cheap (PK lookup).
        row = _db_get_message_by_id(message_id)
        recipient = row.get("recipient_id") if row else "*"

        _event_bus_shim.publish(
            recipient,
            "message.delivered",
            {
                "message_id": message_id,
                "delivered": delivered,
            },
        )
        return True

    def mark_read_for_recipient(self, recipient_id: str) -> int:
        """Mark every unread message for ``recipient_id`` as read.

        Returns the count touched. Publishes ``"message.read"`` only
        when the count is non-zero — a no-op fetch (every message
        already read) must not generate spurious wake-ups, since
        subscribers would re-render the inbox for no reason.
        """
        n = _db_mark_read_for_recipient(recipient_id)
        if n > 0:
            _event_bus_shim.publish(
                recipient_id,
                "message.read",
                {"recipient_id": recipient_id, "count": n},
            )
        return n

    # --- Write interface: delete ----------------------------------------

    def delete(self, message_id: str) -> bool:
        """DELETE a message row.

        Returns True on success, False if the row didn't exist or the
        DB raised. Matches the legacy semantics.

        No ``message.deleted`` publish today — the legacy module-of-
        functions doesn't publish on delete either, and no subscriber
        currently consumes that event. If a future feature needs it
        (e.g. dashboard live-removal of a row), add the publish here
        and the contract test will pick it up.
        """
        return _db_delete_message(message_id)


__all__ = ["MessageRepository"]
