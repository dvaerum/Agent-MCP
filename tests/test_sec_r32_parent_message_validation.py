"""PF-R32-1 — an unvalidated ``parent_message_id`` is a data-integrity bug.

``parent_message_id`` (the migration-0012 self-FK on ``agent_messages``)
is never validated against an existing message before the INSERT. A
well-formed but NONEXISTENT ``parent_message_id`` violates the self-FK;
``MessageRepository.send()`` catches the IntegrityError and returns
``None`` (swallows it). The two send surfaces then mishandle that
``None`` in OPPOSITE, both-wrong ways:

* **MCP ``send_agent_message``** discards ``send()``'s return value,
  commits the audit-log row, and reports ``isError=false`` "Message
  stored" with a ``message_id`` — while the message row was never
  inserted. Silent data-loss + false success + an orphan audit row.
* **REST ``POST /api/messages``** surfaces the same swallowed failure
  as an unhandled 500.

The fix validates parent existence BEFORE the INSERT and raises a
distinct ``ParentMessageNotFound`` so both surfaces return a clean,
consistent "parent not found": MCP → an error ToolResult (never a
false success, no orphan audit/delivery rows); REST → 404 (not 500).

Load-bearing invariants pinned here:
  1. MCP: a nonexistent parent → error, AND neither a message row nor a
     ``send_message`` audit row is persisted.
  2. REST: a nonexistent parent → 404 (not 500), no false audit entry.
  3. Regression: a VALID parent still sends on both paths; a top-level
     message (no parent) still sends on both paths.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# --- direct-DB helpers ---------------------------------------------------


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


def _count_messages_from(sender: str) -> int:
    conn = sqlite3.connect(_db_path())
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE sender_id = ?",
            (sender,),
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


def _latest_message_id_from(sender: str) -> str:
    conn = sqlite3.connect(_db_path())
    try:
        row = conn.execute(
            "SELECT message_id FROM agent_messages WHERE sender_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (sender,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no message rows for sender={sender}"
    return row[0]


def _post(admin, **fields):
    return admin.post("/api/messages", json=dict(fields))


def _query_ids(admin) -> list[str]:
    r = admin.post(
        "/api/messages/query", json={}
    )
    assert r.status_code == 200, r.text
    return [m["message_id"] for m in r.json()["messages"]]


_NONEXISTENT = "msg_does_not_exist_0123456789abcdef"


# --- MCP send_agent_message ---------------------------------------------


async def test_mcp_nonexistent_parent_is_error_and_drops_nothing(
    tmp_path,
) -> None:
    """MCP: a nonexistent ``parent_message_id`` must return an ERROR and
    persist NOTHING — no message row, no orphan ``send_message`` audit
    row. On origin/main the tool reports ``isError=false`` "Message
    stored" while the row was silently dropped (an audit row survives)."""
    content = "orphan-parent body — must never be stored"
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        audit_before = _count_send_audit_rows()

        await admin.call(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": content,
                "parent_message_id": _NONEXISTENT,
                "deliver_method": "store",
            },
        )

        assert getattr(admin, "_last_is_error", False) is True, (
            "send_agent_message with a nonexistent parent reported success"
        )
        assert _count_messages_by_content(content) == 0, (
            "message with a nonexistent parent was silently stored"
        )
        assert _count_send_audit_rows() == audit_before, (
            "a failed send left an orphan audit row behind"
        )


async def test_mcp_valid_parent_still_sends(tmp_path) -> None:
    """Regression: a reply to an EXISTING message still sends."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "root body",
                "subject": "Root Topic",
                "deliver_method": "store",
            },
        )
        root_id = _latest_message_id_from("admin")

        reply_body = "valid-parent reply body"
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": reply_body,
                "parent_message_id": root_id,
                "deliver_method": "store",
            },
        )
        assert _count_messages_by_content(reply_body) == 1


async def test_mcp_top_level_message_still_sends(tmp_path) -> None:
    """Regression: a top-level message (no parent) still sends."""
    body = "top-level body no parent"
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": body,
                "deliver_method": "store",
            },
        )
        assert _count_messages_by_content(body) == 1


# --- REST POST /api/messages --------------------------------------------


async def test_rest_nonexistent_parent_is_404_not_500(tmp_path) -> None:
    """REST: a nonexistent ``parent_message_id`` must return 404 (parent
    not found), NOT 500. On origin/main the swallowed FK violation
    surfaces as an unhandled 500."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post(
            admin,
            recipient_id="alice",
            message_content="rest orphan parent",
            parent_message_id=_NONEXISTENT,
        )
        assert r.status_code == 404, r.text
        assert r.json().get("success") is not True, r.text
        assert _count_messages_by_content("rest orphan parent") == 0


async def test_rest_valid_parent_still_sends(tmp_path) -> None:
    """Regression: a reply to an EXISTING message still stores (200)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        root = _post(
            admin,
            recipient_id="alice",
            message_content="rest root body",
            subject="rest root subject",
        )
        assert root.status_code == 200, root.text
        root_id = root.json()["message_id"]

        reply = _post(
            admin,
            recipient_id="alice",
            message_content="rest reply body",
            parent_message_id=root_id,
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["message_id"] in _query_ids(admin)


async def test_rest_top_level_message_still_sends(tmp_path) -> None:
    """Regression: a top-level message (no parent) still stores (200)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post(
            admin,
            recipient_id="alice",
            message_content="rest top-level body",
        )
        assert r.status_code == 200, r.text
        assert r.json()["message_id"] in _query_ids(admin)
