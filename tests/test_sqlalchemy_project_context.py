"""ORM model + Alembic infrastructure for project_context (Phase 7a/7b).

Post-Phase-7b the model must mirror the migrated sqlite schema:
    context_key TEXT PRIMARY KEY
    value TEXT NOT NULL
    description TEXT
    created_at TEXT
    created_by TEXT
    updated_at TEXT NOT NULL
    updated_by TEXT NOT NULL

This test confirms the SQLAlchemy model can read/write rows against
the same DB that `init_database()` set up via raw SQL, and that
`alembic upgrade head` is idempotent (re-runs are no-ops on an
already-migrated schema).

Phase F (prancy-napping-pie): the memories-tool/REST-endpoint
coverage this file used to carry (exercised via
`tests.harness.mcp_session`) was deleted along with
`agent_mcp.tools.project_context_tools`/`agent_mcp.app.routers.composition`
themselves, superseded by `conexus-tools::project_context_tools`/
`conexus-backend`'s REST surface. What remains here is pure
ORM/schema/Alembic-infrastructure coverage, rewritten to boot the DB
layer directly instead of the now-retired full app stack.
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

    from agent_mcp.db.migrations_runner import run_migrations_upgrade
    run_migrations_upgrade()


def test_project_context_model_round_trip(tmp_path) -> None:
    """The ORM model can write a row and read it back identically.

    Uses the same DB file `_bootstrap_fresh_db` populated, so any
    schema/typing mismatch with the raw-SQL `init_database()` shows
    up immediately.
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import ProjectContext

    now = _dt.datetime.now().isoformat()
    with get_session() as session:
        row = ProjectContext(
            context_key="orm_round_trip",
            value=json.dumps({"hello": "world"}),
            created_at=now,
            created_by="test-suite",
            updated_at=now,
            updated_by="test-suite",
            description="orm round-trip fixture",
        )
        session.add(row)
        session.commit()

    with get_session() as session:
        fetched = (
            session.query(ProjectContext)
            .filter(ProjectContext.context_key == "orm_round_trip")
            .one_or_none()
        )
        assert fetched is not None
        assert json.loads(fetched.value) == {"hello": "world"}
        assert fetched.updated_by == "test-suite"
        assert fetched.description == "orm round-trip fixture"
        assert fetched.updated_at == now
        assert fetched.created_at == now
        assert fetched.created_by == "test-suite"


def test_project_context_model_columns_match_raw_schema(tmp_path) -> None:
    """ORM model column names must match the raw SQL schema exactly.

    If init_database() ever drifts from the model, this catches it.
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import ProjectContext

    model_cols = {c.name for c in ProjectContext.__table__.columns}
    assert model_cols == {
        "context_key",
        "value",
        "description",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    }, f"ORM columns drifted from raw schema: {model_cols}"

    from agent_mcp.core.config import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    try:
        rows = conn.execute("PRAGMA table_info(project_context)").fetchall()
    finally:
        conn.close()
    sqlite_cols = {r[1] for r in rows}
    assert sqlite_cols == model_cols, (
        f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
    )


def test_alembic_upgrade_head_is_idempotent(tmp_path) -> None:
    """Running `alembic upgrade head` a second time must be a no-op.

    `_bootstrap_fresh_db` already ran it once. We re-run it here and
    assert the schema is unchanged and the alembic_version table
    still holds the same revision.
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.core.config import get_db_path
    from agent_mcp.db.migrations_runner import run_migrations_upgrade

    db_path = str(get_db_path())

    def _schema_snapshot() -> list[tuple[str, str | None]]:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return rows

    def _alembic_version() -> str | None:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    before_schema = _schema_snapshot()
    before_version = _alembic_version()
    assert before_version is not None, (
        "bootstrap should have populated alembic_version"
    )

    run_migrations_upgrade()

    after_schema = _schema_snapshot()
    after_version = _alembic_version()
    assert after_schema == before_schema, "schema mutated on a re-upgrade"
    assert after_version == before_version, (
        "alembic version moved on re-upgrade"
    )


def test_alembic_version_table_present_after_startup(tmp_path) -> None:
    """Bootstrap creates the alembic_version row."""
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.core.config import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='alembic_version'"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "alembic_version table missing after bootstrap"
