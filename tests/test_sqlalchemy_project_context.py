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

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_project_context_model_round_trip(tmp_path) -> None:
    """The ORM model can write a row and read it back identically.

    Uses the same DB file the lifespan-startup populated, so any
    schema/typing mismatch with the raw-SQL `init_database()` shows
    up immediately.
    """
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import ProjectContext

    async with mcp_session(tmp_path):
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


async def test_project_context_model_columns_match_raw_schema(tmp_path) -> None:
    """ORM model column names must match the raw SQL schema exactly.

    If init_database() ever drifts from the model, this catches it.
    """
    from agent_mcp.db.models import ProjectContext

    async with mcp_session(tmp_path):
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
            rows = conn.execute(
                "PRAGMA table_info(project_context)"
            ).fetchall()
        finally:
            conn.close()
        sqlite_cols = {r[1] for r in rows}
        assert sqlite_cols == model_cols, (
            f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
        )


async def test_alembic_upgrade_head_is_idempotent(tmp_path) -> None:
    """Running `alembic upgrade head` a second time must be a no-op.

    Application startup already ran it once (via
    server_lifecycle.application_startup). We re-run it here and
    assert the schema is unchanged and the alembic_version table
    still holds the same revision.
    """
    from agent_mcp.core.config import get_db_path
    from agent_mcp.db.migrations_runner import run_migrations_upgrade

    async with mcp_session(tmp_path):
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
            "lifespan startup should have populated alembic_version"
        )

        run_migrations_upgrade()

        after_schema = _schema_snapshot()
        after_version = _alembic_version()
        assert after_schema == before_schema, (
            "schema mutated on a re-upgrade"
        )
        assert after_version == before_version, (
            "alembic version moved on re-upgrade"
        )


async def test_alembic_version_table_present_after_startup(tmp_path) -> None:
    """Application startup creates the alembic_version row."""
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='alembic_version'"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "alembic_version table missing after lifespan startup"


async def test_project_context_tool_uses_orm(tmp_path) -> None:
    """The view_project_context tool still works after ORM rewrite."""
    async with mcp_session(tmp_path) as admin:
        # Seed via existing memories endpoint (which also uses ORM after this PR).
        seed = admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "orm_tool_check",
                "context_value": {"hello": "from-tool"},
                "description": "see test_project_context_tool_uses_orm",
            },
        )
        assert seed.status_code == 200, seed.text

        result = await admin.call(
            "view_project_context",
            {"context_key": "orm_tool_check"},
        )
        text = result[0].text
        assert "orm_tool_check" in text
        assert "from-tool" in text


async def test_all_data_endpoint_returns_project_context(tmp_path) -> None:
    """/api/all-data still returns the project_context bit after rewrite."""
    async with mcp_session(tmp_path) as admin:
        admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "all_data_orm_probe",
                "context_value": "probe-value",
            },
        )

        all_data = admin.get("/api/all-data")
        assert all_data.status_code == 200
        body = all_data.json()
        assert "context" in body
        keys = {entry.get("context_key") for entry in body["context"]}
        assert "all_data_orm_probe" in keys


async def test_context_data_endpoint_returns_project_context(tmp_path) -> None:
    """/api/context-data still returns the project_context bit after rewrite."""
    async with mcp_session(tmp_path) as admin:
        admin.client.post(
            "/api/memories",
            json={
                "token": admin.admin_token,
                "context_key": "ctx_data_orm_probe",
                "context_value": "probe-value",
            },
        )

        ctx = admin.client.get("/api/context-data")
        assert ctx.status_code == 200
        body = ctx.json()
        keys = {entry.get("context_key") for entry in body}
        assert "ctx_data_orm_probe" in keys
