"""ORM model + parity + behaviour tests for `task_notes` (db-review PR-H).

Mirrors the pattern from tests/test_sqlalchemy_*.py — column +
NOT NULL parity, round-trip, and (because PR-H also ships an
action module + tools) coverage of `add_note`/`edit_note`/
`delete_note` and the three new MCP tools.

A separate test file (test_migration_0009_task_notes.py) covers
the legacy-notes → side-table round-trip.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

from tests.harness import _first_text, mcp_session


pytestmark = pytest.mark.asyncio


def _insert_task(
    task_id: str, *, title: str = "T", assigned_to: str | None = None,
) -> None:
    """Insert a task row via raw SQL (mirrors the helper in
    test_sqlalchemy_task.py).

    SEC Wave-B: ``add_task_note`` gates note authorship on task
    ownership; tests that have a worker author a note pass
    ``assigned_to=<worker_agent_id>`` so the worker owns the task.
    """
    import json as _json

    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                task_id, title, "", assigned_to, "admin", "pending",
                "medium", now, now,
                _json.dumps([]),
                _json.dumps([]),
                _json.dumps([]),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def test_task_note_model_columns_match_raw_schema(tmp_path) -> None:
    from agent_mcp.db.models import TaskNote

    async with mcp_session(tmp_path):
        model_cols = {c.name for c in TaskNote.__table__.columns}
        assert model_cols == {
            "note_id", "task_id", "author", "timestamp", "text",
        }, f"ORM columns drifted from raw schema: {model_cols}"

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute("PRAGMA table_info(task_notes)").fetchall()
        finally:
            conn.close()
        sqlite_cols = {r[1] for r in rows}
        assert sqlite_cols == model_cols, (
            f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
        )


async def test_task_note_model_nullability_matches_raw_schema(
    tmp_path,
) -> None:
    from agent_mcp.db.models import TaskNote

    async with mcp_session(tmp_path):
        model_notnull = {
            c.name
            for c in TaskNote.__table__.columns
            if not c.nullable and not c.primary_key
        }

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute("PRAGMA table_info(task_notes)").fetchall()
        finally:
            conn.close()
        sqlite_notnull = {r[1] for r in rows if r[3] == 1 and r[5] == 0}
        assert sqlite_notnull == model_notnull, (
            f"sqlite NOT NULL {sqlite_notnull} != ORM {model_notnull}"
        )


async def test_task_note_model_round_trip(tmp_path) -> None:
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import TaskNote

    async with mcp_session(tmp_path):
        _insert_task("rt-task")
        now = _dt.datetime.now().isoformat()
        with get_session() as session:
            session.add(
                TaskNote(
                    task_id="rt-task",
                    author="alice",
                    timestamp=now,
                    text="Hello",
                )
            )
            session.commit()

        with get_session() as session:
            row = (
                session.query(TaskNote)
                .filter(TaskNote.task_id == "rt-task")
                .one()
            )
            assert row.author == "alice"
            assert row.text == "Hello"
            assert isinstance(row.note_id, int)


# ---- action module coverage --------------------------------------------------


async def test_add_note_returns_note_id(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        _insert_task("an-task")
        nid = task_notes_db.add_note("an-task", "alice", "first note")
        assert isinstance(nid, int)

        row = task_notes_db.get_note(nid)
        assert row is not None
        assert row["author"] == "alice"
        assert row["text"] == "first note"


async def test_list_notes_orders_by_timestamp(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import TaskNote

    async with mcp_session(tmp_path):
        _insert_task("ln-task")
        # Insert in non-monotonic order via direct ORM writes.
        with get_session() as session:
            session.add(TaskNote(
                task_id="ln-task", author="a",
                timestamp="2026-06-01T10:00:00", text="second",
            ))
            session.add(TaskNote(
                task_id="ln-task", author="b",
                timestamp="2026-05-31T10:00:00", text="first",
            ))
            session.commit()

        rows = task_notes_db.list_notes_for_task("ln-task")
        assert [r["text"] for r in rows] == ["first", "second"]


async def test_edit_note_author_can_edit(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        _insert_task("ea-task")
        nid = task_notes_db.add_note("ea-task", "alice", "original")
        ok, err = task_notes_db.edit_note(
            note_id=nid, requester="alice", new_text="updated",
            is_admin=False,
        )
        assert ok is True
        assert err == ""
        assert task_notes_db.get_note(nid)["text"] == "updated"


async def test_edit_note_non_author_rejected(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        _insert_task("en-task")
        nid = task_notes_db.add_note("en-task", "alice", "original")
        ok, err = task_notes_db.edit_note(
            note_id=nid, requester="bob", new_text="hijacked",
            is_admin=False,
        )
        assert ok is False
        assert "alice" in err
        assert task_notes_db.get_note(nid)["text"] == "original"


async def test_edit_note_admin_can_edit_anyone(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        _insert_task("ad-task")
        nid = task_notes_db.add_note("ad-task", "alice", "original")
        ok, err = task_notes_db.edit_note(
            note_id=nid, requester="admin", new_text="moderated",
            is_admin=True,
        )
        assert ok is True
        assert task_notes_db.get_note(nid)["text"] == "moderated"


async def test_edit_note_missing_returns_error(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        ok, err = task_notes_db.edit_note(
            note_id=9999, requester="alice", new_text="x", is_admin=False,
        )
        assert ok is False
        assert "9999" in err


async def test_delete_note_author_can_delete(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        _insert_task("da-task")
        nid = task_notes_db.add_note("da-task", "alice", "x")
        ok, _ = task_notes_db.delete_note(
            note_id=nid, requester="alice", is_admin=False,
        )
        assert ok is True
        assert task_notes_db.get_note(nid) is None


async def test_delete_note_non_author_rejected(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        _insert_task("dn-task")
        nid = task_notes_db.add_note("dn-task", "alice", "x")
        ok, err = task_notes_db.delete_note(
            note_id=nid, requester="bob", is_admin=False,
        )
        assert ok is False
        assert "alice" in err
        assert task_notes_db.get_note(nid) is not None


# ---- PF-R39-1: oversized note_id (>= 2^63) must not crash --------------------
#
# sqlite3 binds a Python int into the ``TaskNote.note_id == note_id``
# filter; an int outside signed-64-bit range makes the driver raise a
# BARE ``OverflowError`` that escaped the ``except SQLAlchemyError``
# guard. These pin the belt-and-suspenders DB-layer catch: an oversized
# id returns the SAME clean not-found/error result the missing-note path
# returns, never an unhandled crash.

_OVERSIZED_NOTE_ID = 2**63  # 9223372036854775808 — first value past int64
_MAX_NOTE_ID = 2**63 - 1    # 9223372036854775807 — the valid boundary


async def test_get_note_oversized_id_returns_none_not_crash(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        # Must not raise OverflowError; treated as "no such note".
        assert task_notes_db.get_note(_OVERSIZED_NOTE_ID) is None


async def test_edit_note_oversized_id_returns_error_not_crash(
    tmp_path,
) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        ok, err = task_notes_db.edit_note(
            note_id=_OVERSIZED_NOTE_ID, requester="alice",
            new_text="x", is_admin=False,
        )
        assert ok is False
        assert isinstance(err, str) and err  # clean, non-empty message


async def test_delete_note_oversized_id_returns_error_not_crash(
    tmp_path,
) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        ok, err = task_notes_db.delete_note(
            note_id=_OVERSIZED_NOTE_ID, requester="alice", is_admin=False,
        )
        assert ok is False
        assert isinstance(err, str) and err


async def test_edit_note_max_int64_id_still_not_found(tmp_path) -> None:
    """Regression: the valid signed-64-bit boundary must keep returning
    a clean not-found (it must NOT be swept up by the overflow guard)."""
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        ok, err = task_notes_db.edit_note(
            note_id=_MAX_NOTE_ID, requester="alice",
            new_text="x", is_admin=False,
        )
        assert ok is False
        assert str(_MAX_NOTE_ID) in err  # "Note <id> not found"


async def test_get_note_max_int64_id_returns_none(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path):
        assert task_notes_db.get_note(_MAX_NOTE_ID) is None


# ---- tool surface ------------------------------------------------------------


async def test_add_task_note_tool_creates_note(tmp_path) -> None:
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path) as admin:
        _insert_task("tool-task-1")

        result = await admin.assert_tool_succeeds(
            "add_task_note",
            {"task_id": "tool-task-1", "text": "via tool"},
        )
        text = result[0].text
        assert "added" in text.lower()

        notes = task_notes_db.list_notes_for_task("tool-task-1")
        assert len(notes) == 1
        assert notes[0]["text"] == "via tool"
        # Author is "admin" because the harness's admin session
        # resolves get_agent_id(admin_token) -> "admin".
        assert notes[0]["author"] == "admin"


async def test_edit_task_note_tool_admin_edits_worker_note(
    tmp_path,
) -> None:
    """Admin must be able to edit a worker-authored note (PR-H
    moderation contract)."""
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path) as admin:
        _insert_task("tool-task-2", assigned_to="alice")
        alice = await admin.create_worker("alice")
        # Alice authors a note.
        await alice.assert_tool_succeeds(
            "add_task_note",
            {"task_id": "tool-task-2", "text": "alice's note"},
        )
        notes = task_notes_db.list_notes_for_task("tool-task-2")
        nid = notes[0]["note_id"]
        assert notes[0]["author"] == "alice"

        # Admin moderates.
        await admin.assert_tool_succeeds(
            "edit_task_note",
            {"note_id": nid, "text": "[moderated]"},
        )
        assert task_notes_db.get_note(nid)["text"] == "[moderated]"


async def test_delete_task_note_tool_non_author_blocked(tmp_path) -> None:
    """Bob can't delete Alice's note via the tool surface."""
    from agent_mcp.db.actions import task_notes_db

    async with mcp_session(tmp_path) as admin:
        _insert_task("tool-task-3", assigned_to="alice")
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        await alice.assert_tool_succeeds(
            "add_task_note",
            {"task_id": "tool-task-3", "text": "alice's"},
        )
        nid = task_notes_db.list_notes_for_task("tool-task-3")[0]["note_id"]

        result = await bob.call(
            "delete_task_note", {"note_id": nid},
        )
        text = result[0].text
        assert "error" in text.lower() or "alice" in text.lower()
        # Note still exists.
        assert task_notes_db.get_note(nid) is not None


async def test_edit_task_note_tool_rejects_oversized_id(tmp_path) -> None:
    """PF-R39-1: an oversized note_id (>= 2^63) must be rejected cleanly
    by the schema ``maximum`` clamp at dispatch — a well-formed
    validation error, NOT an unhandled OverflowError surfacing as a
    generic "Tool execution failed"."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "edit_task_note", {"note_id": _OVERSIZED_NOTE_ID, "text": "x"},
        )
        assert admin._last_is_error is True
        text = _first_text(result).lower()
        assert "validation" in text or "maximum" in text or "note_id" in text
        # The crash signature must NOT be present.
        assert "tool execution failed" not in text
        assert "overflow" not in text


async def test_delete_task_note_tool_rejects_oversized_id(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "delete_task_note", {"note_id": _OVERSIZED_NOTE_ID},
        )
        assert admin._last_is_error is True
        text = _first_text(result).lower()
        assert "validation" in text or "maximum" in text or "note_id" in text
        assert "tool execution failed" not in text
        assert "overflow" not in text


async def test_edit_task_note_tool_max_int64_id_not_found(tmp_path) -> None:
    """Regression: the valid boundary (2^63 - 1) still passes schema
    validation and reaches the DB, returning a clean not-found."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "edit_task_note", {"note_id": _MAX_NOTE_ID, "text": "x"},
        )
        text = _first_text(result).lower()
        # Not-found is a clean typed result — not the crash signature.
        assert "not found" in text or str(_MAX_NOTE_ID) in text
        assert "tool execution failed" not in text
        assert "overflow" not in text
