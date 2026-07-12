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
   :func:`agent_mcp.app.routers.messages.list_messages_api_route`
   (Candidate 3 folding from the architecture review): one entry
   point for both the dashboard query route AND the MCP
   ``get_agent_messages`` tool.

Co-existence with PR #137 module-of-functions:

The old module-of-functions ``agent_mcp.core.repositories.message_repo``
stays alive — every call site that imports the module form keeps
working with no edits. The class form is the new canonical surface;
existing-call-site migration follows in PR 6 of the series.

PR 9 of the architecture-review series — the "Message flip". Until
this PR the class delegated DB I/O to the helpers in
``agent_mcp.db.actions.agent_messages_db``: handler → repo →
db/actions → SQL. Two layers, not one — the "single ownership" the
class claims in its docstring did not match the code.

After the flip:

* All read/write SQL bodies live here (module-level functions for the
  legacy free-function API, instance methods for the EventBus-aware
  surface).
* ``agent_mcp.db.actions.agent_messages_db`` was a re-export shim that
  kept legacy importers (``app.routes`` broadcast,
  ``core.repositories.message_repo`` free-function form,
  ``tests/test_sqlalchemy_agent_message.py``,
  ``tests/test_repository_message.py``) working unchanged.
  arch-deepening R3 #2b deleted the shim and repointed every importer
  at this module directly.
* ``MessageRepository`` is the single owner — handler → repo → SQL.
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from sqlalchemy import (
    delete as sa_delete,
    distinct,
    func,
    insert as sa_insert,
    or_,
    select,
    update as sa_update,
)
from sqlalchemy.exc import SQLAlchemyError

from ..core.config import logger
# NOTE: we import the bus shim lazily inside the publish call sites
# below via ``_publish``. Historically a top-level
# ``from ..core.repositories import _event_bus_shim`` would execute
# ``core.repositories.__init__``, which eagerly imported the legacy
# module-of-functions ``core.repositories.message_repo``, which in
# turn imported ``db.actions.agent_messages_db`` — a shim that
# re-exported from THIS module — producing a circular import at first
# load. arch-deepening R3 #2a deleted ``message_repo`` and #2b deleted
# ``db.actions.agent_messages_db`` outright (closing that cycle for
# good) and relocated the bus shim to ``core.event_bus_shim``. Kept
# lazy anyway: mirrors the pattern PR #153 (Task flip) / PR #154
# (Agent flip) introduced, and a function-local import costs nothing
# at this call frequency.
from ..db.engine import get_session
from ..db.models import Agent, AgentMessage


class ParentMessageNotFound(LookupError):
    """A supplied ``parent_message_id`` matches no existing message.

    Raised by :meth:`MessageRepository.send` BEFORE the INSERT when a
    caller threads a reply onto a parent that doesn't exist (PF-R32-1).
    Without this up-front check the INSERT violated the migration-0012
    self-FK, ``send`` swallowed the ``IntegrityError`` into an ambiguous
    ``None`` return, and the two send surfaces mishandled it in opposite
    both-wrong ways: the MCP tool discarded the ``None`` and reported a
    false success (silent data-loss + an orphan audit row), and the REST
    route surfaced a 500.

    Distinct type — not the bare ``LookupError`` :meth:`send` raises for
    an unknown *recipient* — so each send surface can map it to a
    parent-specific error (MCP ``NotFound(resource="parent message")``;
    REST 404 "Parent message not found") without reusing the recipient
    messaging. Subclasses ``LookupError`` so a generic not-found handler
    still treats it as a 404-class failure.
    """

    def __init__(self, parent_message_id: Any) -> None:
        self.parent_message_id = parent_message_id
        super().__init__(
            f"parent message not found: {parent_message_id!r} does not "
            f"match any existing message. A reply's parent_message_id "
            f"must reference a message that already exists."
        )


def _publish(addressee: str, event: str, payload: Dict[str, Any]) -> None:
    """Lazy-import shim around ``event_bus_shim.publish``.

    See the module-level NOTE above the imports for why this stays a
    function-local import (a stale cycle this used to dodge, closed by
    arch-deepening R3 #2a/#2b).
    """
    from ..core import event_bus_shim

    event_bus_shim.publish(addressee, event, payload)


# ---------------------------------------------------------------------------
# Module-level helpers — formerly lived in db/actions/agent_messages_db.py.
# The class methods below consume these; the ``db.actions.agent_messages_db``
# re-export shim that used to also consume them was deleted in
# arch-deepening R3 #2b.
#
# Behaviour is byte-for-byte identical to the pre-flip helpers (same
# SQLAlchemy ORM model, same commit semantics, same logging shape).
# Tests pin the helpers via their public names; renaming any of these
# would break the shim's re-export contract.
# ---------------------------------------------------------------------------


def _message_to_dict(row: AgentMessage) -> Dict[str, Any]:
    """Project an ``AgentMessage`` ORM row into the dict shape consumers
    expect. Mirrors the pre-cutover ``dict(sqlite_row)`` projection."""
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
        # v5.0.22 message-threads + subjects.
        "subject": row.subject,
        "parent_message_id": row.parent_message_id,
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
    ``INSERT ... VALUES (...), (...), ...`` round-trip via SQLAlchemy's
    Core ``insert()`` + executemany pattern from PR #98).

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
            # v5.0.22 — both default to None when not present so
            # broadcast callers (every fan-out is a root, no reply
            # threading) don't have to think about them. The route /
            # tool layers compute the effective subject before calling
            # this helper if Ollama auto-fill is desired.
            "subject": r.get("subject"),
            "parent_message_id": r.get("parent_message_id"),
        })

    if not payload:
        return 0

    try:
        with get_session() as session:
            # ``executemany`` semantics: pass a list of dicts to
            # ``session.execute(insert(...), payload)``. SQLAlchemy
            # batches them under the hood. With multi-row INSERT,
            # ``result.rowcount`` is not always populated (it's an
            # IteratorResult under the hood); rely on ``len(payload)``
            # for the count since the all-or-nothing transaction
            # guarantees either every row landed or none did.
            session.execute(sa_insert(AgentMessage), payload)
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
    """Flip the ``delivered`` flag on a message. Returns False if the
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
    """Flip ``read=1`` on every unread message for a recipient.

    Returns the number of rows touched (sqlite rowcount). Matches the
    behaviour of the inline ``UPDATE agent_messages SET read = 1 WHERE
    recipient_id = ? AND read = 0`` in ``agent_communication_tools``.
    """
    try:
        with get_session() as session:
            result = session.execute(
                sa_update(AgentMessage)
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


def mark_read_by_ids(
    message_ids: Iterable[str],
    recipient_id: Optional[str] = None,
) -> int:
    """Flip ``read=1`` on exactly the given ``message_ids`` (that are
    still unread).

    Scoped counterpart to :func:`mark_read_for_recipient`. Where that
    function flips *every* unread row for a recipient, this flips only
    the enumerated ids — so a filtered / paged inbox fetch marks just
    the rows the caller actually saw, never control messages truncated
    by a ``LIMIT`` or excluded by a ``message_type`` filter.

    ``recipient_id`` (when supplied) additionally constrains the UPDATE
    to that recipient as defense-in-depth: a caller can never flip the
    read flag on another agent's message by passing its id.

    Returns the number of rows touched (0 for an empty id list).
    """
    ids = [m for m in message_ids if m]
    if not ids:
        return 0
    try:
        with get_session() as session:
            stmt = (
                sa_update(AgentMessage)
                .where(AgentMessage.message_id.in_(ids))
                .where(AgentMessage.read.is_(False))
            )
            if recipient_id is not None:
                stmt = stmt.where(
                    AgentMessage.recipient_id == recipient_id
                )
            result = session.execute(stmt.values(read=True))
            session.commit()
            return result.rowcount if result.rowcount != -1 else 0
    except SQLAlchemyError as e:
        logger.error(
            f"Database error marking messages read by id: {e}",
            exc_info=True,
        )
        return 0
    except Exception as e:
        logger.error(
            f"Unexpected error marking messages read by id: {e}",
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
                sa_delete(AgentMessage).where(
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


# Aliases used inside the class methods below — keeps the existing
# in-class call sites (which reference ``_db_*`` names) compiling
# without rewriting their bodies.
_db_get_message_by_id = get_message_by_id
_db_delete_message = delete_message
_db_mark_delivered = mark_delivered
_db_mark_read_for_recipient = mark_read_for_recipient
_db_mark_read_by_ids = mark_read_by_ids


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

    # --- Invariant helpers ----------------------------------------------

    @staticmethod
    def _recipient_exists(
        recipient_id: Any,
        *,
        connection: Any = None,
    ) -> bool:
        """Return True iff ``recipient_id`` is a legitimate message
        recipient: a live agent row, a tombstone row
        (``[deleted-<id>]`` with ``status='tombstone'``), or the
        special ``'admin'`` label.

        Wave 4 (migration 0014) deleted the synthetic ``agent_id=
        'admin'`` row from the ``agents`` table — before Wave 4 the
        admin recipient resolved via a normal SELECT against agents,
        which is no longer true. The ``'admin'`` label is kept as a
        legitimate destination so worker→admin escalation messages
        continue to be sendable; the database doesn't enforce a
        parent row for it, so this method has to short-circuit on
        the literal value.

        Three connection shapes tolerated so the check works on every
        path :meth:`send` is reachable from:

        * ``None`` — open our own session.
        * SQLAlchemy ``Session`` — query against the caller's session.
        * sqlite3 ``Cursor`` — query against the caller's cursor so the
          existence check participates in the caller's open transaction.
        """
        if not isinstance(recipient_id, str) or not recipient_id:
            return False

        # Wave 4: the 'admin' actor label has no agents-table parent.
        # Treat it as a valid recipient unconditionally — the dashboard
        # surfaces these via the operator messages feed.
        if recipient_id == "admin":
            return True

        # sqlite3 cursor path.
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            try:
                cur.execute(
                    "SELECT 1 FROM agents WHERE agent_id = ? LIMIT 1",
                    (recipient_id,),
                )
                return cur.fetchone() is not None
            except Exception as e:  # pragma: no cover - defensive
                logger.error(
                    f"Database error checking recipient existence for "
                    f"{recipient_id!r} via shared cursor: {e}",
                    exc_info=True,
                )
                return False

        # SQLAlchemy session path (caller-provided OR standalone).
        if connection is not None:
            session = connection
            try:
                row = (
                    session.query(Agent.agent_id)
                    .filter(Agent.agent_id == recipient_id)
                    .one_or_none()
                )
                return row is not None
            except SQLAlchemyError as e:  # pragma: no cover - defensive
                logger.error(
                    f"Database error checking recipient existence for "
                    f"{recipient_id!r} via shared session: {e}",
                    exc_info=True,
                )
                return False

        try:
            with get_session() as session:
                row = (
                    session.query(Agent.agent_id)
                    .filter(Agent.agent_id == recipient_id)
                    .one_or_none()
                )
                return row is not None
        except SQLAlchemyError as e:  # pragma: no cover - defensive
            logger.error(
                f"Database error checking recipient existence for "
                f"{recipient_id!r}: {e}",
                exc_info=True,
            )
            return False

    @staticmethod
    def _parent_message_exists(
        parent_message_id: Any,
        *,
        connection: Any = None,
    ) -> bool:
        """Return True iff ``parent_message_id`` names an existing message.

        The threading self-FK (migration 0012) means a reply must point
        at a real parent row. Validating up front — instead of letting
        the INSERT trip the FK and swallowing the error — is what lets
        :meth:`send` raise a distinct :class:`ParentMessageNotFound`
        (PF-R32-1).

        Same three connection shapes as :meth:`_recipient_exists` so the
        check participates in the caller's open transaction on every path
        :meth:`send` is reachable from:

        * ``None`` — open our own session.
        * SQLAlchemy ``Session`` — query against the caller's session.
        * sqlite3 ``Cursor`` — query against the caller's cursor so the
          existence check sees rows written earlier in the same
          transaction and holds no separate connection lock.
        """
        if not isinstance(parent_message_id, str) or not parent_message_id:
            return False

        # sqlite3 cursor path.
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            try:
                cur.execute(
                    "SELECT 1 FROM agent_messages "
                    "WHERE message_id = ? LIMIT 1",
                    (parent_message_id,),
                )
                return cur.fetchone() is not None
            except Exception as e:  # pragma: no cover - defensive
                logger.error(
                    f"Database error checking parent-message existence "
                    f"for {parent_message_id!r} via shared cursor: {e}",
                    exc_info=True,
                )
                return False

        # SQLAlchemy session path (caller-provided OR standalone).
        if connection is not None:
            session = connection
            try:
                row = (
                    session.query(AgentMessage.message_id)
                    .filter(AgentMessage.message_id == parent_message_id)
                    .one_or_none()
                )
                return row is not None
            except SQLAlchemyError as e:  # pragma: no cover - defensive
                logger.error(
                    f"Database error checking parent-message existence "
                    f"for {parent_message_id!r} via shared session: {e}",
                    exc_info=True,
                )
                return False

        try:
            with get_session() as session:
                row = (
                    session.query(AgentMessage.message_id)
                    .filter(AgentMessage.message_id == parent_message_id)
                    .one_or_none()
                )
                return row is not None
        except SQLAlchemyError as e:  # pragma: no cover - defensive
            logger.error(
                f"Database error checking parent-message existence for "
                f"{parent_message_id!r}: {e}",
                exc_info=True,
            )
            return False

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
        *,
        oldest_first: bool = False,
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

        Returns a timestamp-DESC list of message dicts by default (the
        shape the dashboard message-list and agent-detail sample
        callers expect). Pass ``oldest_first=True`` for timestamp-ASC
        order — the agent event feed (:func:`agent_mcp.tools.
        agent_communication_tools._collect_events_for`) needs a
        contiguous OLDEST-first prefix from its cursor so a backlog
        larger than ``limit`` drains in order across successive polls
        without dropping the oldest tail (BL-R20-1). The dashboard's
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

                order_col = (
                    AgentMessage.timestamp.asc()
                    if oldest_first
                    else AgentMessage.timestamp.desc()
                )
                rows = (
                    session.execute(
                        stmt.order_by(order_col)
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
        connection: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """INSERT a single message + publish ``"message.created"``.

        Returns the freshly-stored dict on success, ``None`` if the
        insert failed (DB error, FK violation, etc.) — matches the
        legacy module-of-functions semantics so callers that today
        branch on a falsy return don't need to change.

        ``connection`` is the transaction-aware seam. Tolerates a
        SQLAlchemy ``Session`` OR a raw ``sqlite3.Cursor`` so
        ``send_agent_message_tool_impl`` and
        ``create_message_api_route`` can keep their multi-table
        writes (message INSERT + tmux delivery flag UPDATE + audit
        log INSERT) atomic. When ``None``, the method opens its own
        session.

        ``subject`` and ``parent_message_id`` are the v5.0.22 threading
        fields. Root messages carry an optional subject; replies
        (``parent_message_id`` set) always have ``subject = None``.
        The threading-policy decision (Ollama-suggested vs. truncated
        body vs. explicit) is owned by the *caller*.

        Raises ``LookupError`` if ``recipient_id`` is neither a live
        agent row, a tombstone row (``[deleted-<id>]``), nor the
        literal ``'admin'`` label. VM e2e on 2026-06-16 surfaced that
        ``send_agent_message`` to a typo'd recipient silently succeeded
        ("Message stored; recipient has no active session"), bypassing
        the PR #138 FK contract. Wave 4 (migration 0014) deleted the
        admin pseudo-agent row from ``agents``; the ``'admin'``
        recipient label survives as a special-cased valid destination
        (worker→admin escalations), but the underlying FK constraint
        is gone — :meth:`_recipient_exists` short-circuits on the
        literal value to preserve that capability.
        """
        # VM e2e fix 2026-06-16: recipient must be a legitimate
        # destination. Wave 4 special-cases the 'admin' label; all
        # other recipients are validated against the agents table.
        # Raise BEFORE any DB write so no partial state is left behind
        # in the caller's wider transaction.
        if not self._recipient_exists(recipient_id, connection=connection):
            raise LookupError(
                f"recipient not found: {recipient_id!r} is not a known "
                f"agent. The recipient must be a live agent, the "
                f"`admin` pseudo-agent, or a tombstone row "
                f"(`[deleted-<id>]`)."
            )

        # PF-R32-1: a supplied parent_message_id must reference an
        # existing message. The migration-0012 self-FK would otherwise
        # trip on INSERT and get swallowed into an ambiguous None return
        # — which the MCP send path discards (false success, silent
        # data-loss) and the REST path maps to a 500. Validate up front
        # and raise a DISTINCT error so both surfaces return a clean
        # parent-not-found. Raise BEFORE any DB write so no partial state
        # is left behind in the caller's wider transaction.
        if parent_message_id is not None and not self._parent_message_exists(
            parent_message_id, connection=connection
        ):
            raise ParentMessageNotFound(parent_message_id)

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            try:
                cur.execute(
                    """
                    INSERT INTO agent_messages (
                        message_id, sender_id, recipient_id,
                        message_content, message_type, priority,
                        timestamp, delivered, read,
                        subject, parent_message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id, sender_id, recipient_id,
                        message_content, message_type, priority,
                        timestamp, delivered, read,
                        subject, parent_message_id,
                    ),
                )
            except Exception as e:
                logger.error(
                    f"Database error inserting message '{message_id}' "
                    f"via shared cursor: {e}", exc_info=True,
                )
                return None
            fresh = {
                "message_id": message_id,
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message_content": message_content,
                "message_type": message_type,
                "priority": priority,
                "timestamp": timestamp,
                "delivered": bool(delivered),
                "read": bool(read),
                "subject": subject,
                "parent_message_id": parent_message_id,
            }
        elif connection is not None:
            session = connection
            try:
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
                    subject=subject,
                    parent_message_id=parent_message_id,
                )
                session.add(row)
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error inserting message '{message_id}' "
                    f"via shared session: {e}", exc_info=True,
                )
                return None
            fresh = {
                "message_id": message_id,
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message_content": message_content,
                "message_type": message_type,
                "priority": priority,
                "timestamp": timestamp,
                "delivered": bool(delivered),
                "read": bool(read),
                "subject": subject,
                "parent_message_id": parent_message_id,
            }
        else:
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
            # `insert_message` signature; write them in a follow-up
            # session so the caller doesn't have to special-case it.
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

        # EventBus publish only on the standalone path. With a
        # ``connection=`` the caller's transaction is still open; a
        # publish before commit could be observed by subscribers
        # (e.g. wait_for_events long-poll) BEFORE the message row is
        # visible to other connections, or could persist after a
        # rollback. Caller owns the publish post-commit if needed.
        if connection is None:
            _publish(
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
            _publish(
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

    def mark_delivered(
        self,
        message_id: str,
        delivered: bool = True,
        *,
        connection: Any = None,
    ) -> bool:
        """Flip the ``delivered`` flag and publish ``"message.delivered"``.

        Returns False if the row didn't exist or the DB call errored.
        The publish only fires on success — a failed flip can't notify
        subscribers about state that didn't change.

        ``connection`` is the transaction-aware seam. Used by
        ``send_agent_message_tool_impl`` to flip the delivered flag
        after a successful tmux delivery inside the same transaction
        as the original INSERT.
        """
        recipient: str = "*"

        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                "SELECT recipient_id FROM agent_messages "
                "WHERE message_id = ?",
                (message_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            try:
                recipient = row["recipient_id"]
            except (KeyError, IndexError):
                recipient = row[0]
            cur.execute(
                "UPDATE agent_messages SET delivered = ? "
                "WHERE message_id = ?",
                (delivered, message_id),
            )
            if cur.rowcount == 0:
                return False
        elif connection is not None:
            session = connection
            try:
                row = (
                    session.query(AgentMessage)
                    .filter(AgentMessage.message_id == message_id)
                    .one_or_none()
                )
                if row is None:
                    return False
                recipient = row.recipient_id
                row.delivered = delivered
                session.flush()
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error marking '{message_id}' delivered "
                    f"via shared session: {e}", exc_info=True,
                )
                return False
        else:
            ok = _db_mark_delivered(message_id, delivered)
            if not ok:
                return False
            row = _db_get_message_by_id(message_id)
            recipient = row.get("recipient_id") if row else "*"

        # EventBus publish only on standalone path — caller's
        # transaction is still open when ``connection=`` is supplied.
        if connection is None:
            _publish(
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
            _publish(
                recipient_id,
                "message.read",
                {"recipient_id": recipient_id, "count": n},
            )
        return n

    def mark_read_by_ids(
        self,
        message_ids: Iterable[str],
        *,
        recipient_id: Optional[str] = None,
    ) -> int:
        """Mark exactly ``message_ids`` as read; publish ``message.read``.

        Scoped counterpart to :meth:`mark_read_for_recipient` for the
        ``get_agent_messages`` fetch path: only the rows the caller
        actually saw (post filter / limit) are flipped, so unread
        control messages truncated by the page are never silently lost.

        ``recipient_id`` scopes the UPDATE to that recipient (defense-
        in-depth) AND keys the ``message.read`` publish — subscribers'
        long-poll waiters consume the event by recipient_id. The publish
        fires only when at least one row was touched, matching
        :meth:`mark_read_for_recipient`'s no-spurious-wake contract.
        """
        n = _db_mark_read_by_ids(message_ids, recipient_id)
        if n > 0 and recipient_id is not None:
            _publish(
                recipient_id,
                "message.read",
                {"recipient_id": recipient_id, "count": n},
            )
        return n

    def mark_read(
        self,
        message_id: str,
        read: bool = True,
        *,
        connection: Any = None,
    ) -> bool:
        """Flip the ``read`` flag on a single message.

        Returns False if the row didn't exist or the DB call errored.
        Distinct from :meth:`mark_read_for_recipient` which operates
        on every unread row for a recipient — this is the
        "I, the dashboard PATCH handler, want to flip THIS one message"
        surface that ``patch_message_api_route`` needs to share its
        cursor with the audit log INSERT.

        No EventBus publish — the dashboard PATCH flow is initiated by
        admin, not a worker, and there is no subscriber today that
        would benefit from a per-message read event (the
        ``message.read`` event the bulk method publishes is keyed on
        recipient_id, which is what the long-poll waiters consume).
        Keeping this method publish-free matches the legacy raw
        UPDATE's behaviour byte-for-byte; subscribers that DO want a
        per-message-read signal in future can opt in here.

        ``connection`` is the transaction-aware seam — the cursor
        shape is what ``patch_message_api_route`` already holds.
        """
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                "UPDATE agent_messages SET read = ? "
                "WHERE message_id = ?",
                (1 if read else 0, message_id),
            )
            return (cur.rowcount or 0) > 0
        if connection is not None:
            session = connection
            try:
                row = (
                    session.query(AgentMessage)
                    .filter(AgentMessage.message_id == message_id)
                    .one_or_none()
                )
                if row is None:
                    return False
                row.read = read
                session.flush()
                return True
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error marking '{message_id}' read "
                    f"via shared session: {e}", exc_info=True,
                )
                return False
        # Standalone path: open our own session.
        try:
            with get_session() as session:
                result = session.execute(
                    sa_update(AgentMessage)
                    .where(AgentMessage.message_id == message_id)
                    .values(read=read)
                )
                session.commit()
                return (result.rowcount or 0) > 0
        except SQLAlchemyError as e:
            logger.error(
                f"Database error marking '{message_id}' read: {e}",
                exc_info=True,
            )
            return False

    # --- Write interface: prune ------------------------------------------

    def prune_read_before(self, cutoff_timestamp: str) -> int:
        """DELETE every ``read=1`` message whose ``timestamp < cutoff``.

        Returns the number of rows deleted. Used by the per-project
        message-retention pruner (:mod:`agent_mcp.features.message_retention`)
        which today owns a raw ``DELETE FROM agent_messages WHERE read = 1
        AND timestamp < ?`` against the connection pool. Routing it
        through the repo keeps the only DELETE against this table in
        one place, which matters because adding a ``message.deleted``
        publish (none today; deliberate per :meth:`delete`) would
        need to fire here too.

        No EventBus publish — the pruner is a background sweep that
        runs every 24h on the read-and-old tail; per-row wake-ups
        would just spam subscribers with rows that have been read for
        days (and their inboxes have long since re-rendered without
        them).

        On DB error returns ``0`` and logs at error.
        """
        try:
            with get_session() as session:
                result = session.execute(
                    sa_delete(AgentMessage)
                    .where(AgentMessage.read.is_(True))
                    .where(AgentMessage.timestamp < cutoff_timestamp)
                )
                session.commit()
                return result.rowcount or 0
        except SQLAlchemyError as e:
            logger.error(
                f"Database error pruning read messages older than "
                f"'{cutoff_timestamp}': {e}",
                exc_info=True,
            )
            return 0
        except Exception as e:
            logger.error(
                f"Unexpected error pruning read messages: {e}",
                exc_info=True,
            )
            return 0

    # --- Write interface: rename_participant ----------------------------

    def rename_participant(
        self,
        old_id: str,
        new_id: str,
        *,
        connection: Any = None,
    ) -> int:
        """Rewrite ``sender_id`` and ``recipient_id`` from ``old_id`` to ``new_id``.

        Used by the agent purge-cascade in
        :func:`agent_mcp.app.routers.agents.purge_agent_api_route` to
        tombstone the rows that reference a deleted agent. Returns
        the total count of rows touched across both columns.

        ``connection`` is the transaction-aware seam: when the caller
        already holds an open SQLAlchemy ``Session`` (or a
        sqlite3-style cursor — see below), the rename happens inside
        that transaction so the wider cascade stays atomic. When
        ``None`` (the standalone case), the method opens its own
        session and commits.

        Two connection shapes are tolerated:

        * **SQLAlchemy Session** — used directly via ORM queries; the
          caller owns commit. This is the going-forward path the other
          repos already support.
        * **sqlite3 Cursor (DB-API)** — used via ``cursor.execute``.
          The purge-cascade route holds a raw sqlite3 cursor today
          inside a hand-rolled ``BEGIN``/``COMMIT`` block; supporting
          the cursor shape lets that migration land without rewriting
          the entire cascade to SQLAlchemy first.

        No EventBus publish — the rename is a tombstoning operation
        not a real message event; subscribers shouldn't see it.
        """
        if not old_id or not new_id:
            return 0

        # sqlite3 cursor path: detect by absence of `query` (Session)
        # / `execute` only attribute. SQLAlchemy Session has `query`;
        # sqlite3.Cursor only has `execute`.
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                "UPDATE agent_messages SET sender_id = ? "
                "WHERE sender_id = ?",
                (new_id, old_id),
            )
            n_sender = cur.rowcount if cur.rowcount != -1 else 0
            cur.execute(
                "UPDATE agent_messages SET recipient_id = ? "
                "WHERE recipient_id = ?",
                (new_id, old_id),
            )
            n_recipient = cur.rowcount if cur.rowcount != -1 else 0
            return n_sender + n_recipient

        if connection is not None:
            # SQLAlchemy Session path: caller commits.
            session = connection
            try:
                r1 = session.execute(
                    sa_update(AgentMessage)
                    .where(AgentMessage.sender_id == old_id)
                    .values(sender_id=new_id)
                )
                r2 = session.execute(
                    sa_update(AgentMessage)
                    .where(AgentMessage.recipient_id == old_id)
                    .values(recipient_id=new_id)
                )
                return (r1.rowcount or 0) + (r2.rowcount or 0)
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error renaming participant "
                    f"'{old_id}' -> '{new_id}' via shared session: {e}",
                    exc_info=True,
                )
                return 0

        # Standalone path: open our own session and commit.
        try:
            with get_session() as session:
                r1 = session.execute(
                    sa_update(AgentMessage)
                    .where(AgentMessage.sender_id == old_id)
                    .values(sender_id=new_id)
                )
                r2 = session.execute(
                    sa_update(AgentMessage)
                    .where(AgentMessage.recipient_id == old_id)
                    .values(recipient_id=new_id)
                )
                session.commit()
                return (r1.rowcount or 0) + (r2.rowcount or 0)
        except SQLAlchemyError as e:
            logger.error(
                f"Database error renaming participant "
                f"'{old_id}' -> '{new_id}': {e}",
                exc_info=True,
            )
            return 0

    # --- Read interface: list_participants ------------------------------

    def list_participants(
        self, *, limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Return the participant lists that source the Messages-tab dropdowns.

        Reads agents whose status is neither ``terminated`` nor
        ``tombstone`` (the live set the dashboard already exposes
        elsewhere) plus a synthetic ``admin`` row prepended for
        compose-to-admin use. The ``tombstones`` list mines the
        distinct ``sender_id`` / ``recipient_id`` values starting with
        the ``[deleted-`` marker that the purge cascade writes — so
        the dropdown can still filter historical messages from purged
        agents.

        Replaces the inline raw-SQL in
        :func:`agent_mcp.app.routers.messages.list_participants_api_route`
        1:1.

        ``limit`` (pentest R4-F2): hard-caps ALL three reads in SQL —
        the live-agent ``.all()`` AND the two ``[deleted-`` ``DISTINCT``
        scans over ``agent_messages`` — so a project with thousands of
        agents / tombstone markers can't materialise an unbounded payload
        on every Messages-tab poll. The route passes an already-clamped
        value (``_clamp_section_limit``, ``[1, 5000]``, default 500) so
        the repo never has to import a router helper. ``None`` preserves
        the historical unbounded read for any non-HTTP caller.

        Returns ``{"live": [...], "tombstones": [...]}``. On DB error
        returns ``{"live": [], "tombstones": []}`` and logs at error.
        """
        try:
            with get_session() as session:
                # Live agents — excludes terminated AND tombstone rows.
                # Mirrors the WHERE clause in routes.py exactly.
                live_q = (
                    session.query(Agent.agent_id, Agent.status)
                    .filter(
                        or_(
                            Agent.status.is_(None),
                            Agent.status.notin_(("terminated", "tombstone")),
                        )
                    )
                    .order_by(Agent.agent_id.asc())
                )
                if limit is not None:
                    live_q = live_q.limit(limit)
                live_rows = live_q.all()
                live = [
                    {"agent_id": r.agent_id, "status": r.status}
                    for r in live_rows
                ]
                if not any(
                    (a.get("agent_id") or "").lower() == "admin"
                    for a in live
                ):
                    live.insert(0, {"agent_id": "admin", "status": "system"})

                # Tombstones: distinct sender_id ∪ recipient_id values
                # beginning with "[deleted-". UNION dedupes. Each DISTINCT
                # scan is LIMITed too (R4-F2) so the tombstone side can't
                # full-table-scan agent_messages unbounded.
                senders_q = (
                    session.query(distinct(AgentMessage.sender_id))
                    .filter(AgentMessage.sender_id.like("[deleted-%"))
                )
                recipients_q = (
                    session.query(distinct(AgentMessage.recipient_id))
                    .filter(AgentMessage.recipient_id.like("[deleted-%"))
                )
                if limit is not None:
                    senders_q = senders_q.limit(limit)
                    recipients_q = recipients_q.limit(limit)
                senders = senders_q.all()
                recipients = recipients_q.all()
                tombstones = sorted(
                    {r[0] for r in senders} | {r[0] for r in recipients}
                )
                if limit is not None:
                    tombstones = tombstones[:limit]
                return {"live": live, "tombstones": tombstones}
        except SQLAlchemyError as e:
            logger.error(
                f"Database error listing participants: {e}", exc_info=True,
            )
            return {"live": [], "tombstones": []}
        except Exception as e:
            logger.error(
                f"Unexpected error listing participants: {e}", exc_info=True,
            )
            return {"live": [], "tombstones": []}

    # --- Write interface: delete ----------------------------------------

    def delete(
        self,
        message_id: str,
        *,
        connection: Any = None,
    ) -> bool:
        """DELETE a message row.

        Returns True on success, False if the row didn't exist or the
        DB raised. Matches the legacy semantics.

        ``connection`` is the transaction-aware seam. Used by
        ``patch_message_api_route`` (DELETE branch) so the message
        DELETE + agent_actions audit-log INSERT land atomically.

        No ``message.deleted`` publish today — the legacy module-of-
        functions doesn't publish on delete either, and no subscriber
        currently consumes that event.
        """
        if connection is not None and not hasattr(connection, "query"):
            cur = connection
            cur.execute(
                "DELETE FROM agent_messages WHERE message_id = ?",
                (message_id,),
            )
            return (cur.rowcount or 0) > 0
        if connection is not None:
            session = connection
            try:
                from sqlalchemy import delete as sa_delete
                result = session.execute(
                    sa_delete(AgentMessage).where(
                        AgentMessage.message_id == message_id
                    )
                )
                session.flush()
                return (result.rowcount or 0) > 0
            except SQLAlchemyError as e:
                logger.error(
                    f"Database error deleting '{message_id}' via "
                    f"shared session: {e}", exc_info=True,
                )
                return False
        return _db_delete_message(message_id)


__all__ = ["MessageRepository", "ParentMessageNotFound"]
