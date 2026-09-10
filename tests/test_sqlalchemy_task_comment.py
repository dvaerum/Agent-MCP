"""ORM model + parity tests for `task_comments` (db-review PR-H).

Mirrors the pattern from tests/test_sqlalchemy_*.py -- column +
NOT NULL parity and round-trip.

A separate test file (test_migration_0009_task_notes.py) covers
the legacy-notes -> side-table round-trip (migration 0009 itself
creates the table under its original name, `task_notes`; migration
0026 renames it to `task_comments` -- see that migration's module
docstring).

Phase F (prancy-napping-pie): the action-module + MCP-tool coverage
this file used to carry (`add_comment`/`edit_comment`/`delete_comment`,
exercised via `tests.harness.mcp_session`) was deleted along with
`agent_mcp.db.actions.task_comments_db`/`agent_mcp.tools.task_comments_tools`
themselves, superseded by `conexus-db::task_comments_repository`/
`conexus-tools::task_comments_tools`. What remains here is pure
ORM/schema-parity coverage, rewritten to boot the DB layer directly
instead of the now-retired full app stack.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3


def _bootstrap_fresh_db(tmp_path) -> None:
    """Point the ORM engine at a fresh per-tmpdir DB and run
    init_database() -- the same production bootstrap sequence
    tests/test_migration_*.py already use, with no app/mcp_session
    boot needed."""
    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    os.environ["MCP_PROJECT_DIR"] = project_dir

    from agent_mcp.db import engine as _engine
    _engine._engine = None  # type: ignore[attr-defined]

    from agent_mcp.db.schema import init_database
    init_database()


def _insert_task(task_id: str, *, title: str = "T") -> None:
    """Insert a task row via raw SQL -- task_comments.task_id FKs to
    it, so the round-trip test needs a real parent row."""
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
                task_id, title, "", None, "admin", "pending",
                "medium", now, now,
                _json.dumps([]),
                _json.dumps([]),
                _json.dumps([]),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_task_comment_model_columns_match_raw_schema(tmp_path) -> None:
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import TaskComment

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


def test_task_comment_model_nullability_matches_raw_schema(tmp_path) -> None:
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import TaskComment

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


def test_task_comment_model_round_trip(tmp_path) -> None:
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import TaskComment

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
