"""Schema test for `feat/message-threads-and-subjects` (v5.0.22).

This is the **RED** test for behavior block 1: the `agent_messages`
table gains two new columns and an FK so that root + reply messages
can be modelled as an email-style thread:

* ``subject TEXT NULL`` — root-only summary line.
* ``parent_message_id TEXT NULL`` — FK to ``agent_messages(message_id)``
  with ``ON DELETE SET NULL``; NULL = root message.
* Index ``idx_agent_messages_parent ON agent_messages(parent_message_id)``
  for thread-by-root queries.

Test surface:

* Migration applies cleanly on a fresh DB (lifespan startup runs it).
* Both new columns exist with the expected NULLability.
* INSERT with subject + parent_message_id succeeds when the parent
  is a real row.
* INSERT with a parent_message_id that doesn't reference any row
  fails with a FOREIGN KEY constraint violation (FK enforced).
* Deleting the parent row NULLs out children's ``parent_message_id``
  rather than cascading the DELETE (ON DELETE SET NULL).
* The supporting index is present.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """{column_name: (type, notnull, default, pk)}"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1]: (r[2], bool(r[3]), r[4], bool(r[5])) for r in rows}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def _fk_list(conn: sqlite3.Connection, table: str) -> list[tuple]:
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    # (from_col, ref_table, ref_col, on_update, on_delete)
    return [(r[3], r[2], r[4], r[5], r[6]) for r in rows]


def _insert_root_message(conn: sqlite3.Connection, **overrides) -> str:
    msg_id = overrides.pop("message_id", f"msg_{secrets.token_hex(8)}")
    cols = {
        "message_id": msg_id,
        "sender_id": "admin",
        "recipient_id": "admin",
        "message_content": "body",
        "message_type": "text",
        "priority": "normal",
        "timestamp": datetime.utcnow().isoformat(),
        "delivered": 0,
        "read": 0,
        "subject": None,
        "parent_message_id": None,
    }
    cols.update(overrides)
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO agent_messages ({', '.join(cols.keys())}) "
        f"VALUES ({placeholders})",
        list(cols.values()),
    )
    conn.commit()
    return msg_id


async def test_subject_and_parent_columns_exist(tmp_path) -> None:
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            cols = _columns(conn, "agent_messages")
            assert "subject" in cols, f"missing subject column; have {list(cols)}"
            assert "parent_message_id" in cols, (
                f"missing parent_message_id column; have {list(cols)}"
            )
            # Both should be nullable (NOT NULL flag = False).
            assert cols["subject"][1] is False, "subject should be nullable"
            assert cols["parent_message_id"][1] is False, (
                "parent_message_id should be nullable"
            )
        finally:
            conn.close()


async def test_parent_fk_declared_with_set_null(tmp_path) -> None:
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            fks = _fk_list(conn, "agent_messages")
            # Find the self-referential FK for parent_message_id.
            parent_fk = [
                fk
                for fk in fks
                if fk[0] == "parent_message_id" and fk[1] == "agent_messages"
            ]
            assert parent_fk, (
                f"parent_message_id FK missing from agent_messages; have {fks}"
            )
            # ON DELETE behaviour should be SET NULL (index 4 in our tuple).
            from_col, ref_table, ref_col, _on_update, on_delete = parent_fk[0]
            assert ref_col == "message_id", parent_fk
            assert on_delete.upper() == "SET NULL", (
                f"parent_message_id FK should be ON DELETE SET NULL; "
                f"got on_delete={on_delete!r}"
            )
        finally:
            conn.close()


async def test_parent_index_present(tmp_path) -> None:
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            idx = _indexes(conn, "agent_messages")
            assert "idx_agent_messages_parent" in idx, (
                f"idx_agent_messages_parent missing; have {idx}"
            )
        finally:
            conn.close()


async def test_insert_with_subject_and_parent_succeeds(tmp_path) -> None:
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            root_id = _insert_root_message(
                conn,
                subject="Initial topic",
            )
            reply_id = _insert_root_message(
                conn,
                parent_message_id=root_id,
                subject=None,  # replies have no subject
            )
            row = conn.execute(
                "SELECT subject, parent_message_id FROM agent_messages "
                "WHERE message_id = ?",
                (reply_id,),
            ).fetchone()
            assert row is not None
            assert row[0] is None, "reply subject should be NULL"
            assert row[1] == root_id, "reply parent_message_id should match root"
        finally:
            conn.close()


async def test_insert_with_dangling_parent_rejected(tmp_path) -> None:
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_root_message(
                    conn,
                    parent_message_id="msg_does_not_exist",
                )
        finally:
            conn.close()


async def test_parent_delete_sets_child_to_null(tmp_path) -> None:
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            root_id = _insert_root_message(conn, subject="Root")
            reply_id = _insert_root_message(conn, parent_message_id=root_id)

            conn.execute(
                "DELETE FROM agent_messages WHERE message_id = ?",
                (root_id,),
            )
            conn.commit()

            row = conn.execute(
                "SELECT parent_message_id FROM agent_messages "
                "WHERE message_id = ?",
                (reply_id,),
            ).fetchone()
            assert row is not None, "child row should still exist after parent delete"
            assert row[0] is None, (
                f"child parent_message_id should be NULLed by ON DELETE SET NULL; "
                f"got {row[0]!r}"
            )
        finally:
            conn.close()
