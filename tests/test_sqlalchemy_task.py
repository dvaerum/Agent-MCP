"""ORM model + parity test for the `tasks` table (db-review PR-G3).

Third model in the incremental SQLAlchemy adoption (after
`ProjectContext` and `Agent`). The model must mirror what
`init_database()` creates for fresh DBs; this test catches drift
and pins the read-side cutover of `agent_mcp.db.actions.task_db`.

Mirrors the shape of `tests/test_sqlalchemy_agent.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_task_model_round_trip(tmp_path) -> None:
    """ORM model can write a row and read it back identically."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Task

    async with mcp_session(tmp_path):
        now = _dt.datetime.now().isoformat()
        with get_session() as session:
            row = Task(
                task_id="orm_round_trip_task",
                title="Round trip",
                description="Round trip test task",
                assigned_to=None,
                created_by="admin",
                status="pending",
                priority="medium",
                created_at=now,
                updated_at=now,
                parent_task=None,
                child_tasks=json.dumps(["child-1"]),
                depends_on_tasks=json.dumps(["dep-1"]),
                notes=json.dumps([
                    {"timestamp": now, "author": "admin", "content": "n1"},
                ]),
            )
            session.add(row)
            session.commit()

        with get_session() as session:
            fetched = (
                session.query(Task)
                .filter(Task.task_id == "orm_round_trip_task")
                .one_or_none()
            )
            assert fetched is not None
            assert fetched.title == "Round trip"
            assert fetched.created_by == "admin"
            assert fetched.status == "pending"
            assert fetched.priority == "medium"
            assert json.loads(fetched.child_tasks) == ["child-1"]
            assert json.loads(fetched.depends_on_tasks) == ["dep-1"]
            notes = json.loads(fetched.notes)
            assert isinstance(notes, list) and notes[0]["content"] == "n1"
            assert fetched.created_at == now


async def test_task_model_columns_match_raw_schema(tmp_path) -> None:
    """ORM model columns must match the raw SQL schema exactly.

    If `init_database()` ever drifts from the model (or a migration
    adds a column without updating the model), this catches it.
    """
    from agent_mcp.db.models import Task

    async with mcp_session(tmp_path):
        model_cols = {c.name for c in Task.__table__.columns}
        assert model_cols == {
            "task_id",
            "title",
            "description",
            "assigned_to",
            "created_by",
            "status",
            "priority",
            "created_at",
            "updated_at",
            "parent_task",
            "child_tasks",
            "depends_on_tasks",
            "notes",
            # Event-coord PR-1 (migration 0010): capability gate for
            # the PR-2 `unassigned_task_appeared` wake event.
            "required_capabilities",
        }, f"ORM columns drifted from raw schema: {model_cols}"

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
        finally:
            conn.close()
        sqlite_cols = {r[1] for r in rows}
        assert sqlite_cols == model_cols, (
            f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
        )


async def test_task_model_nullability_matches_raw_schema(tmp_path) -> None:
    """Per-column NOT NULL flags must match between ORM and raw DDL.

    See `test_sqlalchemy_agent.py` for the PK-exclusion rationale —
    SQLite's PRAGMA reports PK columns with notnull=0 unless the DDL
    explicitly says NOT NULL; SQLAlchemy infers NOT NULL from
    `primary_key=True` regardless.
    """
    from agent_mcp.db.models import Task

    async with mcp_session(tmp_path):
        model_notnull = {
            c.name
            for c in Task.__table__.columns
            if not c.nullable and not c.primary_key
        }

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
        finally:
            conn.close()
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        sqlite_notnull = {r[1] for r in rows if r[3] == 1 and r[5] == 0}
        assert sqlite_notnull == model_notnull, (
            f"sqlite NOT NULL {sqlite_notnull} != ORM {model_notnull}"
        )


def _insert_task(
    task_id: str,
    *,
    title: str = "T",
    description: str = "",
    assigned_to: str | None = None,
    created_by: str = "admin",
    status: str = "pending",
    priority: str = "medium",
) -> None:
    """Insert a task row via raw SQL — bypasses the tool surface so
    the ORM tests don't depend on `assign_task` quirks (task_id is
    server-generated by that tool)."""
    import datetime
    import json as _json

    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                task_id,
                title,
                description,
                assigned_to,
                created_by,
                status,
                priority,
                now,
                now,
                _json.dumps([]),
                _json.dumps([]),
                _json.dumps([]),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def test_task_db_get_task_by_id_uses_orm(tmp_path) -> None:
    """`get_task_by_id` returns the same dict shape after the ORM cutover.

    The dict shape is a soft contract — every callsite indexes by
    string key (e.g. `task['status']`, `task['task_id']`). The ORM
    cutover must preserve that, including JSON deserialisation for
    `notes` / `child_tasks` / `depends_on_tasks`.
    """
    from agent_mcp.db.actions.task_db import get_task_by_id

    async with mcp_session(tmp_path):
        _insert_task("t-by-id", title="Task by id")

        task = get_task_by_id("t-by-id")
        assert task is not None
        assert task["task_id"] == "t-by-id"
        assert task["title"] == "Task by id"
        assert isinstance(task["child_tasks"], list)
        assert isinstance(task["depends_on_tasks"], list)
        assert isinstance(task["notes"], list)


async def test_task_db_get_task_by_id_missing_returns_none(tmp_path) -> None:
    from agent_mcp.db.actions.task_db import get_task_by_id

    async with mcp_session(tmp_path):
        assert get_task_by_id("no-such-task") is None


async def test_task_db_get_all_tasks_uses_orm(tmp_path) -> None:
    from agent_mcp.db.actions.task_db import get_all_tasks_from_db

    async with mcp_session(tmp_path):
        _insert_task("t-all-1", title="T1", priority="low")
        _insert_task("t-all-2", title="T2", priority="high")

        rows = get_all_tasks_from_db()
        ids = {r["task_id"] for r in rows}
        assert {"t-all-1", "t-all-2"}.issubset(ids)
        for r in rows:
            assert isinstance(r["child_tasks"], list)
            assert isinstance(r["depends_on_tasks"], list)
            assert isinstance(r["notes"], list)


async def test_task_db_get_tasks_by_agent_id_uses_orm(tmp_path) -> None:
    from agent_mcp.db.actions.task_db import get_tasks_by_agent_id

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _insert_task("t-alice-1", title="Alice task", assigned_to="alice")

        rows = get_tasks_by_agent_id("alice")
        ids = {r["task_id"] for r in rows}
        assert "t-alice-1" in ids
        for r in rows:
            assert r["assigned_to"] == "alice"


async def test_task_db_get_tasks_by_agent_id_with_status_filter(
    tmp_path,
) -> None:
    from agent_mcp.db.actions.task_db import (
        get_tasks_by_agent_id,
        update_task_fields_in_db,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _insert_task("t-pending", title="Pending", assigned_to="alice")
        _insert_task("t-completed", title="Completed", assigned_to="alice")
        # Mutate one to status=completed via the ORM writer.
        update_task_fields_in_db("t-completed", {"status": "completed"})

        rows = get_tasks_by_agent_id("alice", status_filter="completed")
        ids = {r["task_id"] for r in rows}
        assert "t-completed" in ids
        assert "t-pending" not in ids


async def test_task_db_update_task_fields_uses_orm(tmp_path) -> None:
    from agent_mcp.db.actions.task_db import (
        get_task_by_id,
        update_task_fields_in_db,
    )

    async with mcp_session(tmp_path):
        _insert_task("t-update", title="Initial")

        before = get_task_by_id("t-update")
        assert before is not None
        assert before["title"] == "Initial"

        ok = update_task_fields_in_db(
            "t-update", {"title": "After", "status": "in_progress"},
        )
        assert ok is True

        after = get_task_by_id("t-update")
        assert after is not None
        assert after["title"] == "After"
        assert after["status"] == "in_progress"
        # updated_at must have advanced (we always rewrite it).
        assert after["updated_at"] >= before["updated_at"]


async def test_task_db_update_task_fields_json_serialises(tmp_path) -> None:
    """JSON-typed fields (notes/child_tasks/depends_on_tasks) must be
    serialised before being written to the TEXT column."""
    from agent_mcp.db.actions.task_db import (
        get_task_by_id,
        update_task_fields_in_db,
    )

    async with mcp_session(tmp_path):
        _insert_task("t-json", title="JSON")

        ok = update_task_fields_in_db(
            "t-json",
            {
                "child_tasks": ["child-a", "child-b"],
                "depends_on_tasks": ["dep-a"],
            },
        )
        assert ok is True

        after = get_task_by_id("t-json")
        assert after is not None
        assert after["child_tasks"] == ["child-a", "child-b"]
        assert after["depends_on_tasks"] == ["dep-a"]


async def test_task_db_update_task_fields_rejects_unknown_field(
    tmp_path,
) -> None:
    """Unsupported field names must be skipped (anti-injection guard).

    The historical behaviour for unknown-only-fields was to return
    False (no valid columns to update). Preserve that.
    """
    from agent_mcp.db.actions.task_db import update_task_fields_in_db

    async with mcp_session(tmp_path):
        _insert_task("t-bad", title="X")
        ok = update_task_fields_in_db(
            "t-bad", {"; DROP TABLE tasks; --": "x"},
        )
        assert ok is False


async def test_task_db_update_task_fields_missing_task_returns_false(
    tmp_path,
) -> None:
    from agent_mcp.db.actions.task_db import update_task_fields_in_db

    async with mcp_session(tmp_path):
        ok = update_task_fields_in_db(
            "no-such-task", {"status": "completed"},
        )
        assert ok is False
