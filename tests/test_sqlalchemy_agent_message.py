"""ORM model + parity test for the `agent_messages` table (db-review PR-G4).

Fourth model in the incremental SQLAlchemy adoption (after
`ProjectContext`, `Agent`, `Task`). The model must mirror what
`init_database()` creates for fresh DBs; this test catches drift
and pins the read/write cutover of the new
`agent_mcp.db.actions.agent_messages_db` action module.

Mirrors the shape of `tests/test_sqlalchemy_task.py` (PR-G3).
Coverage:

* Column + NOT NULL parity vs the raw DDL.
* Round-trip via the ORM.
* The new action-layer functions: `insert_message`,
  `bulk_insert_messages` (executemany path, per PR #98), `mark_delivered`,
  `mark_read_for_recipient`, `delete_message`, `get_message_by_id`,
  `count_unread_for_recipient`.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _now() -> str:
    return _dt.datetime.now().isoformat()


async def test_agent_message_model_round_trip(tmp_path) -> None:
    """ORM model can write a row and read it back identically."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import AgentMessage

    async with mcp_session(tmp_path) as admin:
        # Need real agents because PR-G1 (migration 0008) declares
        # NOT NULL FKs from agent_messages.{sender,recipient} → agents.
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        now = _now()
        with get_session() as session:
            row = AgentMessage(
                message_id="msg-round-trip",
                sender_id="alice",
                recipient_id="bob",
                message_content="hello",
                message_type="text",
                priority="normal",
                timestamp=now,
                delivered=False,
                read=False,
            )
            session.add(row)
            session.commit()

        with get_session() as session:
            fetched = (
                session.query(AgentMessage)
                .filter(AgentMessage.message_id == "msg-round-trip")
                .one_or_none()
            )
            assert fetched is not None
            assert fetched.sender_id == "alice"
            assert fetched.recipient_id == "bob"
            assert fetched.message_content == "hello"
            assert fetched.message_type == "text"
            assert fetched.priority == "normal"
            assert fetched.delivered is False
            assert fetched.read is False


async def test_agent_message_model_columns_match_raw_schema(tmp_path) -> None:
    from agent_mcp.db.models import AgentMessage

    async with mcp_session(tmp_path):
        model_cols = {c.name for c in AgentMessage.__table__.columns}
        assert model_cols == {
            "message_id",
            "sender_id",
            "recipient_id",
            "message_content",
            "message_type",
            "priority",
            "timestamp",
            "delivered",
            "read",
            # v5.0.22: message threads + subjects (migration 0012).
            "subject",
            "parent_message_id",
        }, f"ORM columns drifted from raw schema: {model_cols}"

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute(
                "PRAGMA table_info(agent_messages)"
            ).fetchall()
        finally:
            conn.close()
        sqlite_cols = {r[1] for r in rows}
        assert sqlite_cols == model_cols, (
            f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
        )


async def test_agent_message_model_nullability_matches_raw_schema(
    tmp_path,
) -> None:
    """Per-column NOT NULL flags must match between ORM and raw DDL."""
    from agent_mcp.db.models import AgentMessage

    async with mcp_session(tmp_path):
        model_notnull = {
            c.name
            for c in AgentMessage.__table__.columns
            if not c.nullable and not c.primary_key
        }

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute(
                "PRAGMA table_info(agent_messages)"
            ).fetchall()
        finally:
            conn.close()
        sqlite_notnull = {r[1] for r in rows if r[3] == 1 and r[5] == 0}
        assert sqlite_notnull == model_notnull, (
            f"sqlite NOT NULL {sqlite_notnull} != ORM {model_notnull}"
        )


async def test_insert_message_writes_row(tmp_path) -> None:
    from agent_mcp.db.actions.agent_messages_db import (
        get_message_by_id,
        insert_message,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        ok = insert_message(
            message_id="msg-1",
            sender_id="alice",
            recipient_id="bob",
            message_content="hi",
            message_type="text",
            priority="normal",
            timestamp=_now(),
        )
        assert ok is True

        row = get_message_by_id("msg-1")
        assert row is not None
        assert row["sender_id"] == "alice"
        assert row["recipient_id"] == "bob"
        assert row["message_content"] == "hi"
        assert row["delivered"] is False
        assert row["read"] is False


async def test_bulk_insert_messages_uses_executemany_pattern(
    tmp_path,
) -> None:
    """Bulk insert path mirrors PR #98 — many recipients, one
    executemany. The behavioural contract is just 'all rows show up',
    independent of the SQL surface, so this test exercises the
    public function and counts the rows."""
    from agent_mcp.db.actions.agent_messages_db import (
        bulk_insert_messages,
        get_message_by_id,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        await admin.create_worker("carol")

        ts = _now()
        rows = [
            {
                "message_id": "msg-bulk-1",
                "sender_id": "alice",
                "recipient_id": "bob",
                "message_content": "hi bob",
                "message_type": "text",
                "priority": "normal",
                "timestamp": ts,
            },
            {
                "message_id": "msg-bulk-2",
                "sender_id": "alice",
                "recipient_id": "carol",
                "message_content": "hi carol",
                "message_type": "text",
                "priority": "normal",
                "timestamp": ts,
            },
        ]
        inserted = bulk_insert_messages(rows)
        assert inserted == 2

        for r in rows:
            persisted = get_message_by_id(r["message_id"])
            assert persisted is not None
            assert persisted["recipient_id"] == r["recipient_id"]
            assert persisted["message_content"] == r["message_content"]


async def test_mark_delivered_flips_flag(tmp_path) -> None:
    from agent_mcp.db.actions.agent_messages_db import (
        get_message_by_id,
        insert_message,
        mark_delivered,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        insert_message(
            message_id="msg-d",
            sender_id="alice",
            recipient_id="bob",
            message_content="x",
            message_type="text",
            priority="normal",
            timestamp=_now(),
        )

        ok = mark_delivered("msg-d", True)
        assert ok is True

        row = get_message_by_id("msg-d")
        assert row is not None
        assert row["delivered"] is True


async def test_mark_read_for_recipient_only_flips_unread(tmp_path) -> None:
    """`mark_read_for_recipient` flips read=1 on all unread messages
    addressed to a given recipient and returns the count touched."""
    from agent_mcp.db.actions.agent_messages_db import (
        bulk_insert_messages,
        get_message_by_id,
        mark_read_for_recipient,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        await admin.create_worker("carol")

        ts = _now()
        bulk_insert_messages([
            {
                "message_id": "m1",
                "sender_id": "alice",
                "recipient_id": "bob",
                "message_content": "to bob",
                "message_type": "text",
                "priority": "normal",
                "timestamp": ts,
            },
            {
                "message_id": "m2",
                "sender_id": "alice",
                "recipient_id": "carol",
                "message_content": "to carol",
                "message_type": "text",
                "priority": "normal",
                "timestamp": ts,
            },
        ])

        n = mark_read_for_recipient("bob")
        assert n == 1

        assert get_message_by_id("m1")["read"] is True
        # carol's message must NOT be flipped.
        assert get_message_by_id("m2")["read"] is False


async def test_count_unread_for_recipient(tmp_path) -> None:
    from agent_mcp.db.actions.agent_messages_db import (
        bulk_insert_messages,
        count_unread_for_recipient,
        mark_read_for_recipient,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        ts = _now()
        bulk_insert_messages([
            {
                "message_id": f"m-{i}",
                "sender_id": "alice",
                "recipient_id": "bob",
                "message_content": f"hi {i}",
                "message_type": "text",
                "priority": "normal",
                "timestamp": ts,
            }
            for i in range(3)
        ])

        assert count_unread_for_recipient("bob") == 3
        mark_read_for_recipient("bob")
        assert count_unread_for_recipient("bob") == 0


async def test_delete_message_removes_row(tmp_path) -> None:
    from agent_mcp.db.actions.agent_messages_db import (
        delete_message,
        get_message_by_id,
        insert_message,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        insert_message(
            message_id="m-del",
            sender_id="alice",
            recipient_id="bob",
            message_content="bye",
            message_type="text",
            priority="normal",
            timestamp=_now(),
        )

        assert delete_message("m-del") is True
        assert get_message_by_id("m-del") is None
        # idempotent / no-op on missing.
        assert delete_message("m-del") is False
