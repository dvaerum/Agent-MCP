"""ORM model + parity test for the `agents` table (db-review PR-G2).

Second model in the incremental SQLAlchemy adoption that started
with `ProjectContext`. The model must mirror what `init_database()`
creates for fresh DBs.

Phase F (prancy-napping-pie): the repository-cutover tests this file
used to carry (`get_agent_by_id`/`get_agent_by_token`/etc., exercised
via `tests.harness.mcp_session`) were deleted along with
`agent_mcp.repositories.agent_repository` itself, superseded by
`conexus-db::agent_repository`. What remains here is pure ORM/schema-
parity coverage, rewritten to boot the DB layer directly (the same
`init_database()` bootstrap `tests/test_migration_*.py` already uses)
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


def test_agent_model_round_trip(tmp_path) -> None:
    """ORM model can write a row and read it back identically."""
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = _dt.datetime.now().isoformat()
    with get_session() as session:
        row = Agent(
            token="tok-round-trip",
            agent_id="orm_round_trip",
            created_at=now,
            status="active",
            current_task=None,
            working_directory="/tmp",
            color="#abc123",
            updated_at=now,
        )
        session.add(row)
        session.commit()

    with get_session() as session:
        fetched = (
            session.query(Agent)
            .filter(Agent.agent_id == "orm_round_trip")
            .one_or_none()
        )
        assert fetched is not None
        assert fetched.token == "tok-round-trip"
        assert fetched.status == "active"
        assert fetched.working_directory == "/tmp"
        assert fetched.color == "#abc123"
        assert fetched.created_at == now


def test_agent_model_columns_match_raw_schema(tmp_path) -> None:
    """ORM model columns must match the raw SQL schema exactly.

    If `init_database()` ever drifts from the model (or a migration
    adds a column without updating the model), this catches it.
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import Agent

    model_cols = {c.name for c in Agent.__table__.columns}
    assert model_cols == {
        "token",
        "agent_id",
        "created_at",
        "status",
        "current_task",
        "working_directory",
        "color",
        "terminated_at",
        "updated_at",
        "aoe_session_id",
        # Event-coord PR-1 (migration 0010): per-agent wake-loop
        # toggle + cursor for fetch_events_since (PR-2).
        "auto_event_loop",
        "last_event_seen_at",
        # Event-loop idle-stop (migration 0020): wall-clock marker of
        # the agent's last real event, seeded on first listen.
        "last_activity_at",
        # Phase 2 Wave 1a (migration 0013): per-agent privilege
        # tier. Read by @requires_role in Wave 2; column-only in
        # this PR.
        "agent_role",
        # Agent self-service profiles (migration 0018): free-text
        # profile + review/change bookkeeping.
        "profile",
        "profile_updated_at",
        "profile_reviewed_at",
        "profile_updated_by",
    }, f"ORM columns drifted from raw schema: {model_cols}"

    from agent_mcp.core.config import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    try:
        rows = conn.execute("PRAGMA table_info(agents)").fetchall()
    finally:
        conn.close()
    sqlite_cols = {r[1] for r in rows}
    assert sqlite_cols == model_cols, (
        f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
    )


def test_agent_model_nullability_matches_raw_schema(tmp_path) -> None:
    """Per-column NOT NULL flags must match between ORM and raw DDL.

    Drift here is silent: the ORM might accept a NULL the SQL would
    reject (or vice versa) and tests pass until production hits the
    edge case.

    SQLite's PRAGMA reports PK columns with notnull=0 unless the DDL
    explicitly declares NOT NULL. SQLAlchemy infers NOT NULL from
    `primary_key=True` regardless. Exclude PK columns from the
    comparison so we're testing nullability of non-PK columns
    (which is what the SQL DDL actually controls).
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import Agent

    model_notnull = {
        c.name
        for c in Agent.__table__.columns
        if not c.nullable and not c.primary_key
    }

    from agent_mcp.core.config import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    try:
        rows = conn.execute("PRAGMA table_info(agents)").fetchall()
    finally:
        conn.close()
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
    sqlite_notnull = {r[1] for r in rows if r[3] == 1 and r[5] == 0}
    assert sqlite_notnull == model_notnull, (
        f"sqlite NOT NULL {sqlite_notnull} != ORM {model_notnull}"
    )
