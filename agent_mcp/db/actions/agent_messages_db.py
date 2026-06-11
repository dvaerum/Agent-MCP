"""Reusable DB operations for the ``agent_messages`` table.

.. deprecated:: PR 9 of the architecture-review series (the "Message
   flip"; follow-up to #146 / #147 / #153 / #154).

   The SQL bodies that used to live here have moved into
   :mod:`agent_mcp.repositories.message_repository`. This module is
   kept as a thin compatibility surface so existing call sites
   (``app.routes`` broadcast loop, the older module-of-functions repo
   under ``agent_mcp.core.repositories.message_repo``,
   ``tests/test_sqlalchemy_agent_message.py``, the read-side pins in
   ``tests/test_repository_message.py``) keep working with no edits.

   New code should import :class:`agent_mcp.repositories.MessageRepository`
   via the ``message_repo`` singleton instead — it's the single owner
   of the message DB writes + EventBus publishing, so subscribers
   don't have to poll the DB to notice new messages.

The shim re-exports the same public symbols (function signatures,
return shapes, and even the underscore-prefixed ``_message_to_dict``
helper) so importers don't have to change. Pre-flip behaviour
(no EventBus publish — that's the repo's job) is unchanged.
"""

from __future__ import annotations

from ...repositories.message_repository import (
    _message_to_dict,
    bulk_insert_messages,
    count_unread_for_recipient,
    delete_message,
    get_message_by_id,
    insert_message,
    mark_delivered,
    mark_read_for_recipient,
)

__all__ = [
    "bulk_insert_messages",
    "count_unread_for_recipient",
    "delete_message",
    "get_message_by_id",
    "insert_message",
    "mark_delivered",
    "mark_read_for_recipient",
    # Underscore-prefixed export preserved because
    # ``agent_mcp.repositories.message_repository`` (pre-flip) imported
    # it from here. Post-flip the direction is reversed, but external
    # code that happened to import it still works.
    "_message_to_dict",
]
