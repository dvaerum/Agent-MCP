"""ORM model + parity test for the `tasks` table (db-review PR-G3).

Third model in the incremental SQLAlchemy adoption (after
`ProjectContext` and `Agent`). The model must mirror what
`init_database()` creates for fresh DBs.

Phase F (prancy-napping-pie): the repository-cutover tests this file
used to carry (exercised via `tests.harness.mcp_session`) were
deleted along with `agent_mcp.repositories.task_repository` itself,
superseded by `conexus-db::task_repository`. What remains here is
pure ORM/schema-parity coverage, rewritten to boot the DB layer
directly instead of the now-retired full app stack.
"""

from __future__ import annotations

import datetime as _dt
import json
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


def test_task_model_round_trip(tmp_path) -> None:
    """ORM model can write a row and read it back identically."""
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Task

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


def test_task_model_columns_match_raw_schema(tmp_path) -> None:
    """ORM model columns must match the raw SQL schema exactly.

    If `init_database()` ever drifts from the model (or a migration
    adds a column without updating the model), this catches it.
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import Task

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


def test_task_model_nullability_matches_raw_schema(tmp_path) -> None:
    """Per-column NOT NULL flags must match between ORM and raw DDL.

    See `test_sqlalchemy_agent.py` for the PK-exclusion rationale --
    SQLite's PRAGMA reports PK columns with notnull=0 unless the DDL
    explicitly says NOT NULL; SQLAlchemy infers NOT NULL from
    `primary_key=True` regardless.
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import Task

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
