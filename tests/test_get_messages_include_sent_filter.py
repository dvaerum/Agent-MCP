"""include_sent=False must not leak sent messages.

Bug report (v5.0.21): a worker called
``get_agent_messages(include_sent=False, include_received=True)`` and
saw messages they had themselves sent.

The MCP tool's SQL at ``agent_communication_tools.py:329-331`` reads::

    elif include_received:
        query_conditions.append("recipient_id = ?")
        query_params.append(agent_id)

so on inspection the SQL is correct. This test reproduces the
end-to-end call as the sender AND as the recipient to pin the
contract: an agent looking at their inbox with `include_sent=False`
must see only messages where they are the recipient.

A parallel REST check exercises ``POST /api/messages/query``: that
endpoint uses ``from``/``to`` filter semantics (no ``include_sent``
flag); we assert that a ``to=A`` filter returns only messages
addressed to A, not messages A sent. That's the analog of "not
leaking sent" for the REST surface.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _result_text(blocks) -> str:
    parts = []
    for b in blocks:
        text = getattr(b, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _seed_message(
    message_id: str,
    sender_id: str,
    recipient_id: str,
    content: str,
) -> None:
    """Seed a single agent_messages row directly via SQL."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO agent_messages "
            "(message_id, sender_id, recipient_id, message_content, "
            " message_type, priority, timestamp, delivered, read) "
            "VALUES (?, ?, ?, ?, 'text', 'normal', ?, 1, 0)",
            (message_id, sender_id, recipient_id, content, now),
        )
        conn.commit()
    finally:
        conn.close()


async def test_sender_with_include_sent_false_does_not_see_own_sent(
    tmp_path,
) -> None:
    """As Alice (sender), calling get_agent_messages with
    include_sent=False, include_received=True must NOT return the
    message Alice sent to Bob.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")

        _seed_message(
            "msg-from-alice",
            sender_id="alice",
            recipient_id="bob",
            content="hello bob from alice",
        )

        result = await alice.call(
            "get_agent_messages",
            {"include_sent": False, "include_received": True},
        )
        text = _result_text(result)
        assert not getattr(alice, "_last_is_error", False), text
        # The message Alice SENT must NOT appear in her view when she
        # asked for only received messages.
        assert "hello bob from alice" not in text, (
            f"include_sent=False leaked the sent message into the "
            f"sender's view; got: {text}"
        )


async def test_recipient_with_include_sent_false_still_sees_received(
    tmp_path,
) -> None:
    """As Bob (recipient), the same filter MUST still return the
    message Alice sent to him.
    """
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        _seed_message(
            "msg-to-bob",
            sender_id="alice",
            recipient_id="bob",
            content="hello bob from alice",
        )

        result = await bob.call(
            "get_agent_messages",
            {"include_sent": False, "include_received": True},
        )
        text = _result_text(result)
        assert not getattr(bob, "_last_is_error", False), text
        assert "hello bob from alice" in text, (
            f"recipient must see received messages with "
            f"include_sent=False; got: {text}"
        )


async def test_rest_messages_query_to_filter_only_returns_addressed(
    tmp_path,
) -> None:
    """POST /api/messages/query with ``to=alice`` must return only
    messages addressed to alice — not messages alice herself sent.

    The REST endpoint uses sender/recipient filters explicitly; the
    bug analog here is whether the ``to=`` filter is applied
    correctly. We assert it is.
    """
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        _seed_message(
            "msg-alice-sent",
            sender_id="alice",
            recipient_id="bob",
            content="from-alice-to-bob",
        )
        _seed_message(
            "msg-alice-received",
            sender_id="bob",
            recipient_id="alice",
            content="from-bob-to-alice",
        )

        resp = admin.client.post(
            "/api/messages/query",
            json={"token": admin.admin_token, "to": "alice"},
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["messages"]
        contents = {r["message_content"] for r in rows}
        assert "from-bob-to-alice" in contents, (
            f"to=alice must include messages addressed to alice; "
            f"got: {contents}"
        )
        assert "from-alice-to-bob" not in contents, (
            f"to=alice must NOT include messages alice sent; "
            f"got: {contents}"
        )
