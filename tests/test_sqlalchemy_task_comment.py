"""ORM model + parity + behaviour tests for `task_comments` (db-review PR-H).

Mirrors the pattern from tests/test_sqlalchemy_*.py — column +
NOT NULL parity, round-trip, and (because PR-H also ships an
action module + tools) coverage of `add_comment`/`edit_comment`/
`delete_comment` and the three new MCP tools.

A separate test file (test_migration_0009_task_notes.py) covers
the legacy-notes → side-table round-trip (migration 0009 itself
creates the table under its original name, `task_notes`; migration
0026 renames it to `task_comments` — see that migration's module
docstring).
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

from tests.harness import _first_text, mcp_session

pytestmark = pytest.mark.asyncio


def _insert_task(
    task_id: str,
    *,
    title: str = "T",
    assigned_to: str | None = None,
    status: str = "pending",
) -> None:
    """Insert a task row via raw SQL (mirrors the helper in
    test_sqlalchemy_task.py).

    SEC Wave-B: ``add_task_comment`` gates comment authorship on task
    ownership; tests that have a worker author a comment pass
    ``assigned_to=<worker_agent_id>`` so the worker owns the task.

    ``status`` (OBS-R12-2): defaults to ``"pending"``; pass a terminal
    value for the terminal-state-guard tests below. Inserted directly
    (never via an UPDATE), so the DB-level guard trigger — which only
    fires on an UPDATE whose OLD row is already terminal — never
    interferes with seeding.
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
                task_id, title, "", assigned_to, "admin", status,
                "medium", now, now,
                _json.dumps([]),
                _json.dumps([]),
                _json.dumps([]),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def test_task_comment_model_columns_match_raw_schema(tmp_path) -> None:
    from agent_mcp.db.models import TaskComment

    async with mcp_session(tmp_path):
        model_cols = {c.name for c in TaskComment.__table__.columns}
        assert model_cols == {
            "note_id", "task_id", "author", "timestamp", "text",
        }, f"ORM columns drifted from raw schema: {model_cols}"

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute("PRAGMA table_info(task_comments)").fetchall()
        finally:
            conn.close()
        sqlite_cols = {r[1] for r in rows}
        assert sqlite_cols == model_cols, (
            f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
        )


async def test_task_comment_model_nullability_matches_raw_schema(
    tmp_path,
) -> None:
    from agent_mcp.db.models import TaskComment

    async with mcp_session(tmp_path):
        model_notnull = {
            c.name
            for c in TaskComment.__table__.columns
            if not c.nullable and not c.primary_key
        }

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute("PRAGMA table_info(task_comments)").fetchall()
        finally:
            conn.close()
        sqlite_notnull = {r[1] for r in rows if r[3] == 1 and r[5] == 0}
        assert sqlite_notnull == model_notnull, (
            f"sqlite NOT NULL {sqlite_notnull} != ORM {model_notnull}"
        )


async def test_task_comment_model_round_trip(tmp_path) -> None:
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import TaskComment

    async with mcp_session(tmp_path):
        _insert_task("rt-task")
        now = _dt.datetime.now().isoformat()
        with get_session() as session:
            session.add(
                TaskComment(
                    task_id="rt-task",
                    author="alice",
                    timestamp=now,
                    text="Hello",
                )
            )
            session.commit()

        with get_session() as session:
            row = (
                session.query(TaskComment)
                .filter(TaskComment.task_id == "rt-task")
                .one()
            )
            assert row.author == "alice"
            assert row.text == "Hello"
            assert isinstance(row.note_id, int)


# ---- action module coverage --------------------------------------------------


async def test_add_comment_returns_note_id(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        _insert_task("an-task")
        nid = task_comments_db.add_comment("an-task", "alice", "first note")
        assert isinstance(nid, int)

        row = task_comments_db.get_comment(nid)
        assert row is not None
        assert row["author"] == "alice"
        assert row["text"] == "first note"


async def test_list_comments_orders_by_timestamp(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import TaskComment

    async with mcp_session(tmp_path):
        _insert_task("ln-task")
        # Insert in non-monotonic order via direct ORM writes.
        with get_session() as session:
            session.add(TaskComment(
                task_id="ln-task", author="a",
                timestamp="2026-06-01T10:00:00", text="second",
            ))
            session.add(TaskComment(
                task_id="ln-task", author="b",
                timestamp="2026-05-31T10:00:00", text="first",
            ))
            session.commit()

        rows = task_comments_db.list_comments_for_task("ln-task")
        assert [r["text"] for r in rows] == ["first", "second"]


async def test_edit_comment_author_can_edit(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        _insert_task("ea-task")
        nid = task_comments_db.add_comment("ea-task", "alice", "original")
        ok, err = task_comments_db.edit_comment(
            note_id=nid, requester="alice", new_text="updated",
            is_admin=False,
        )
        assert ok is True
        assert err == ""
        assert task_comments_db.get_comment(nid)["text"] == "updated"


async def test_edit_comment_non_author_rejected(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        _insert_task("en-task")
        nid = task_comments_db.add_comment("en-task", "alice", "original")
        ok, err = task_comments_db.edit_comment(
            note_id=nid, requester="bob", new_text="hijacked",
            is_admin=False,
        )
        assert ok is False
        assert "alice" in err
        assert task_comments_db.get_comment(nid)["text"] == "original"


async def test_edit_comment_admin_can_edit_anyone(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        _insert_task("ad-task")
        nid = task_comments_db.add_comment("ad-task", "alice", "original")
        ok, _err = task_comments_db.edit_comment(
            note_id=nid, requester="admin", new_text="moderated",
            is_admin=True,
        )
        assert ok is True
        assert task_comments_db.get_comment(nid)["text"] == "moderated"


async def test_edit_comment_missing_returns_error(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        ok, err = task_comments_db.edit_comment(
            note_id=9999, requester="alice", new_text="x", is_admin=False,
        )
        assert ok is False
        assert "9999" in err


async def test_delete_comment_author_can_delete(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        _insert_task("da-task")
        nid = task_comments_db.add_comment("da-task", "alice", "x")
        ok, _ = task_comments_db.delete_comment(
            note_id=nid, requester="alice", is_admin=False,
        )
        assert ok is True
        assert task_comments_db.get_comment(nid) is None


async def test_delete_comment_non_author_rejected(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        _insert_task("dn-task")
        nid = task_comments_db.add_comment("dn-task", "alice", "x")
        ok, err = task_comments_db.delete_comment(
            note_id=nid, requester="bob", is_admin=False,
        )
        assert ok is False
        assert "alice" in err
        assert task_comments_db.get_comment(nid) is not None


# ---- PF-R39-1: oversized note_id (>= 2^63) must not crash --------------------
#
# sqlite3 binds a Python int into the ``TaskComment.note_id == note_id``
# filter; an int outside signed-64-bit range makes the driver raise a
# BARE ``OverflowError`` that escaped the ``except SQLAlchemyError``
# guard. These pin the belt-and-suspenders DB-layer catch: an oversized
# id returns the SAME clean not-found/error result the missing-comment
# path returns, never an unhandled crash.

_OVERSIZED_NOTE_ID = 2**63  # 9223372036854775808 — first value past int64
_MAX_NOTE_ID = 2**63 - 1    # 9223372036854775807 — the valid boundary


async def test_get_comment_oversized_id_returns_none_not_crash(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        # Must not raise OverflowError; treated as "no such comment".
        assert task_comments_db.get_comment(_OVERSIZED_NOTE_ID) is None


async def test_edit_comment_oversized_id_returns_error_not_crash(
    tmp_path,
) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        ok, err = task_comments_db.edit_comment(
            note_id=_OVERSIZED_NOTE_ID, requester="alice",
            new_text="x", is_admin=False,
        )
        assert ok is False
        assert isinstance(err, str) and err  # clean, non-empty message


async def test_delete_comment_oversized_id_returns_error_not_crash(
    tmp_path,
) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        ok, err = task_comments_db.delete_comment(
            note_id=_OVERSIZED_NOTE_ID, requester="alice", is_admin=False,
        )
        assert ok is False
        assert isinstance(err, str) and err


async def test_edit_comment_max_int64_id_still_not_found(tmp_path) -> None:
    """Regression: the valid signed-64-bit boundary must keep returning
    a clean not-found (it must NOT be swept up by the overflow guard)."""
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        ok, err = task_comments_db.edit_comment(
            note_id=_MAX_NOTE_ID, requester="alice",
            new_text="x", is_admin=False,
        )
        assert ok is False
        assert str(_MAX_NOTE_ID) in err  # "Comment <id> not found"


async def test_get_comment_max_int64_id_returns_none(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        assert task_comments_db.get_comment(_MAX_NOTE_ID) is None


# ---- tool surface ------------------------------------------------------------


async def test_add_task_comment_tool_creates_comment(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path) as admin:
        _insert_task("tool-task-1")

        result = await admin.assert_tool_succeeds(
            "add_task_comment",
            {"task_id": "tool-task-1", "text": "via tool"},
        )
        text = result[0].text
        assert "added" in text.lower()

        comments = task_comments_db.list_comments_for_task("tool-task-1")
        assert len(comments) == 1
        assert comments[0]["text"] == "via tool"
        # Author is "admin" because the harness's admin session
        # resolves get_agent_id(admin_token) -> "admin".
        assert comments[0]["author"] == "admin"


async def test_edit_task_comment_tool_admin_edits_worker_comment(
    tmp_path,
) -> None:
    """Admin must be able to edit a worker-authored comment (PR-H
    moderation contract)."""
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path) as admin:
        _insert_task("tool-task-2", assigned_to="alice")
        alice = await admin.create_worker("alice")
        # Alice authors a comment.
        await alice.assert_tool_succeeds(
            "add_task_comment",
            {"task_id": "tool-task-2", "text": "alice's note"},
        )
        comments = task_comments_db.list_comments_for_task("tool-task-2")
        nid = comments[0]["note_id"]
        assert comments[0]["author"] == "alice"

        # Admin moderates.
        await admin.assert_tool_succeeds(
            "edit_task_comment",
            {"note_id": nid, "text": "[moderated]"},
        )
        assert task_comments_db.get_comment(nid)["text"] == "[moderated]"


async def test_delete_task_comment_tool_non_author_blocked(tmp_path) -> None:
    """Bob can't delete Alice's comment via the tool surface."""
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path) as admin:
        _insert_task("tool-task-3", assigned_to="alice")
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        await alice.assert_tool_succeeds(
            "add_task_comment",
            {"task_id": "tool-task-3", "text": "alice's"},
        )
        nid = task_comments_db.list_comments_for_task("tool-task-3")[0]["note_id"]

        result = await bob.call(
            "delete_task_comment", {"note_id": nid},
        )
        text = result[0].text
        assert "error" in text.lower() or "alice" in text.lower()
        # Comment still exists.
        assert task_comments_db.get_comment(nid) is not None


async def test_edit_task_comment_tool_rejects_oversized_id(tmp_path) -> None:
    """PF-R39-1: an oversized note_id (>= 2^63) must be rejected cleanly
    by the schema ``maximum`` clamp at dispatch — a well-formed
    validation error, NOT an unhandled OverflowError surfacing as a
    generic "Tool execution failed"."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "edit_task_comment", {"note_id": _OVERSIZED_NOTE_ID, "text": "x"},
        )
        assert admin._last_is_error is True
        text = _first_text(result).lower()
        assert "validation" in text or "maximum" in text or "note_id" in text
        # The crash signature must NOT be present.
        assert "tool execution failed" not in text
        assert "overflow" not in text


async def test_delete_task_comment_tool_rejects_oversized_id(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "delete_task_comment", {"note_id": _OVERSIZED_NOTE_ID},
        )
        assert admin._last_is_error is True
        text = _first_text(result).lower()
        assert "validation" in text or "maximum" in text or "note_id" in text
        assert "tool execution failed" not in text
        assert "overflow" not in text


async def test_edit_task_comment_tool_max_int64_id_not_found(tmp_path) -> None:
    """Regression: the valid boundary (2^63 - 1) still passes schema
    validation and reaches the DB, returning a clean not-found."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "edit_task_comment", {"note_id": _MAX_NOTE_ID, "text": "x"},
        )
        text = _first_text(result).lower()
        # Not-found is a clean typed result — not the crash signature.
        assert "not found" in text or str(_MAX_NOTE_ID) in text
        assert "tool execution failed" not in text
        assert "overflow" not in text


# ---- OBS-R12-2 (round-13 class-sweep): terminal-state carve-out miss --------
#
# The round-13 pentest sweep found this side table was a THIRD sibling of
# the "terminal-state carve-out miss" bug class BL-R25-1 / R12-F4 / R12-F5
# already fixed on the legacy ``tasks.notes`` JSON-column paths — none of
# ``add_comment``/``edit_comment``/``delete_comment`` (or their tool
# wrappers) checked the parent task's terminal status at all. These tests
# pin the fix: a Python-level check (ordered AFTER the ownership gate, so
# the PF-1 comment-existence oracle isn't reopened) PLUS the DB-level
# guard triggers (migration 0025) as a backstop.


async def test_add_comment_raises_terminal_task_write_blocked(tmp_path) -> None:
    """Direct DB-layer call, bypassing every Python-level check — the
    DB trigger itself refuses the INSERT and the repo translates it
    into a clean, typed exception (never a silent no-op or raw SQL
    leak)."""
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.db.terminal_task_guard import TerminalTaskWriteBlocked

    async with mcp_session(tmp_path):
        _insert_task("tn-terminal-1", status="completed")

        with pytest.raises(TerminalTaskWriteBlocked):
            task_comments_db.add_comment("tn-terminal-1", "alice", "sneaky")

        assert task_comments_db.list_comments_for_task("tn-terminal-1") == []


async def test_edit_comment_returns_clean_error_on_terminal_task(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path):
        _insert_task("tn-terminal-2", status="pending")
        nid = task_comments_db.add_comment("tn-terminal-2", "alice", "original")
        # Drive the task terminal AFTER the comment exists — mirrors a
        # worker completing a task that already carries comments.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE tasks SET status='completed' WHERE task_id=?",
                ("tn-terminal-2",),
            )
            conn.commit()
        finally:
            conn.close()

        ok, err = task_comments_db.edit_comment(
            note_id=nid, requester="alice", new_text="edited",
            is_admin=False,
        )
        assert ok is False
        assert "terminal" in err.lower()
        assert task_comments_db.get_comment(nid)["text"] == "original"


async def test_delete_comment_returns_clean_error_on_terminal_task(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        _insert_task("tn-terminal-3", status="pending")
        nid = task_comments_db.add_comment("tn-terminal-3", "alice", "original")
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE tasks SET status='cancelled' WHERE task_id=?",
                ("tn-terminal-3",),
            )
            conn.commit()
        finally:
            conn.close()

        ok, err = task_comments_db.delete_comment(
            note_id=nid, requester="alice", is_admin=False,
        )
        assert ok is False
        assert "terminal" in err.lower()
        assert task_comments_db.get_comment(nid) is not None


async def test_edit_comment_non_author_still_gets_ownership_error_on_terminal_task(
    tmp_path,
) -> None:
    """PF-1 ordering: a non-owner probing a comment on a TERMINAL task
    must get the SAME "owned by" refusal as on a live task — the
    terminal check must never run (or leak) before the ownership gate,
    or a non-owner could distinguish task status from the error shape."""
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        _insert_task("tn-terminal-4", status="pending")
        nid = task_comments_db.add_comment("tn-terminal-4", "alice", "original")
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE tasks SET status='failed' WHERE task_id=?",
                ("tn-terminal-4",),
            )
            conn.commit()
        finally:
            conn.close()

        ok, err = task_comments_db.edit_comment(
            note_id=nid, requester="bob", new_text="hijacked",
            is_admin=False,
        )
        assert ok is False
        assert "alice" in err
        assert "terminal" not in err.lower()
        assert task_comments_db.get_comment(nid)["text"] == "original"


async def test_add_task_comment_tool_blocked_on_terminal_task(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db

    async with mcp_session(tmp_path) as admin:
        _insert_task("tn-tool-terminal-1", status="completed")

        result = await admin.call(
            "add_task_comment",
            {"task_id": "tn-tool-terminal-1", "text": "sneaky"},
        )
        assert admin._last_is_error is True
        text = _first_text(result).lower()
        assert "terminal" in text
        assert task_comments_db.list_comments_for_task("tn-tool-terminal-1") == []


async def test_edit_task_comment_tool_blocked_on_terminal_task(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        _insert_task("tn-tool-terminal-2", status="pending")
        await admin.assert_tool_succeeds(
            "add_task_comment",
            {"task_id": "tn-tool-terminal-2", "text": "original"},
        )
        nid = task_comments_db.list_comments_for_task("tn-tool-terminal-2")[0]["note_id"]
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE tasks SET status='completed' WHERE task_id=?",
                ("tn-tool-terminal-2",),
            )
            conn.commit()
        finally:
            conn.close()

        result = await admin.call(
            "edit_task_comment", {"note_id": nid, "text": "edited"},
        )
        assert admin._last_is_error is True
        text = _first_text(result).lower()
        assert "terminal" in text
        assert task_comments_db.get_comment(nid)["text"] == "original"


async def test_delete_task_comment_tool_blocked_on_terminal_task(tmp_path) -> None:
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        _insert_task("tn-tool-terminal-3", status="pending")
        await admin.assert_tool_succeeds(
            "add_task_comment",
            {"task_id": "tn-tool-terminal-3", "text": "original"},
        )
        nid = task_comments_db.list_comments_for_task("tn-tool-terminal-3")[0]["note_id"]
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE tasks SET status='cancelled' WHERE task_id=?",
                ("tn-tool-terminal-3",),
            )
            conn.commit()
        finally:
            conn.close()

        result = await admin.call(
            "delete_task_comment", {"note_id": nid},
        )
        assert admin._last_is_error is True
        text = _first_text(result).lower()
        assert "terminal" in text
        assert task_comments_db.get_comment(nid) is not None
