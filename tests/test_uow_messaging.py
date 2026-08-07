"""D2 — the message-send mutation on the write-path unit-of-work.

``send_agent_message`` used to hand-sequence its post-commit
choreography: ``atomic_with_audit`` for the message INSERT + the DB
audit row, then a separate ``g.notify_agent_inbox`` recipient wake and
an in-memory ``log_audit`` write AFTER the block. D2 folds the whole
mutation onto ``with unit_of_work() as u:`` so the recipient wake and
the (now unified) audit are *registered* on ``u`` and flush only after
a successful commit.

Invariants pinned here (both survive the refactor — behavior-preserving
— AND make the emit-iff-commit property structural):

  1. **Committed send** stores exactly one message row, fires EXACTLY
     ONE recipient wake (``notify_agent_inbox``), and writes exactly one
     ``send`` audit row.
  2. **Rolled-back send** (a reply to a NONEXISTENT parent →
     ``ParentMessageNotFound`` inside the scope) fires ZERO side
     effects: no message row, no delivery, no recipient wake, no audit
     row. This is emit-iff-commit AND a re-guard of PF-R32-1's
     silent-drop contract — a failed send must never report success or
     strand orphan audit/delivery state.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _db_path() -> str:
    from agent_mcp.core.config import get_db_path

    return str(get_db_path())


def _count_messages_by_content(content: str) -> int:
    conn = sqlite3.connect(_db_path())
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE message_content = ?",
            (content,),
        ).fetchone()[0]
    finally:
        conn.close()


def _count_send_audit_rows() -> int:
    conn = sqlite3.connect(_db_path())
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM agent_actions "
            "WHERE action_type IN ('send_message', 'send_agent_message')",
            (),
        ).fetchone()[0]
    finally:
        conn.close()


_NONEXISTENT_PARENT = "msg_does_not_exist_deadbeef01234567"


async def test_committed_send_stores_wakes_once_and_audits(
    tmp_path, monkeypatch
) -> None:
    """A committed send stores the message, wakes the recipient EXACTLY
    once, and writes exactly one audit row."""
    from agent_mcp.core import globals as g

    wakes: list[str] = []
    monkeypatch.setattr(
        g, "notify_agent_inbox", lambda agent_id: wakes.append(agent_id)
    )

    content = "uow committed send body"
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        audit_before = _count_send_audit_rows()
        wakes.clear()

        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": content,
                "deliver_method": "store",
            },
        )

        assert _count_messages_by_content(content) == 1, (
            "committed send did not store exactly one message row"
        )
        assert wakes == ["alice"], (
            f"expected exactly one recipient wake for 'alice', got {wakes!r}"
        )
        assert _count_send_audit_rows() == audit_before + 1, (
            "committed send did not write exactly one audit row"
        )


async def test_rolledback_send_fires_zero_side_effects(
    tmp_path, monkeypatch
) -> None:
    """A rolled-back send (nonexistent parent) fires ZERO side effects.

    Proves emit-iff-commit AND re-guards PF-R32-1: the failed send must
    return an error and leave no message row, no recipient wake, and no
    orphan audit row.
    """
    from agent_mcp.core import globals as g

    wakes: list[str] = []
    monkeypatch.setattr(
        g, "notify_agent_inbox", lambda agent_id: wakes.append(agent_id)
    )

    content = "uow rolled-back send body — must never persist"
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        audit_before = _count_send_audit_rows()
        wakes.clear()

        await admin.call(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": content,
                "parent_message_id": _NONEXISTENT_PARENT,
                "deliver_method": "store",
            },
        )

        assert getattr(admin, "_last_is_error", False) is True, (
            "a send to a nonexistent parent reported success"
        )
        assert _count_messages_by_content(content) == 0, (
            "rolled-back send left a message/delivery row behind"
        )
        assert wakes == [], (
            f"rolled-back send fired a recipient wake: {wakes!r}"
        )
        assert _count_send_audit_rows() == audit_before, (
            "rolled-back send left an orphan audit row behind"
        )
