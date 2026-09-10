"""ORM model + parity test for the `agent_messages` table (db-review PR-G4).

Fourth model in the incremental SQLAlchemy adoption (after
`ProjectContext`, `Agent`, `Task`). The model must mirror what
`init_database()` creates for fresh DBs.

Phase F (prancy-napping-pie): the repository-cutover tests this file
used to carry (`insert_message`/`bulk_insert_messages`/`mark_delivered`/
etc., exercised via `tests.harness.mcp_session`) were deleted along
with `agent_mcp.repositories.message_repository` itself, superseded by
`conexus-db::message_repository`. What remains here is pure ORM/schema-
parity coverage, rewritten to boot the DB layer directly instead of
the now-retired full app stack.
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


def _now() -> str:
    return _dt.datetime.now().isoformat()


def _seed_agent(session, agent_id: str) -> None:
    """agent_messages.{sender,recipient} carry NOT NULL FKs to agents
    (migration 0008) -- seed the two real agent rows the round-trip
    test's foreign keys need."""
    from agent_mcp.db.models import Agent

    session.add(
        Agent(
            token=f"tok-{agent_id}",
            agent_id=agent_id,
            created_at=_now(),
            status="active",
            working_directory="/tmp",
        )
    )


def test_agent_message_model_round_trip(tmp_path) -> None:
    """ORM model can write a row and read it back identically."""
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import AgentMessage

    now = _now()
    with get_session() as session:
        _seed_agent(session, "alice")
        _seed_agent(session, "bob")
        session.commit()

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


def test_agent_message_model_columns_match_raw_schema(tmp_path) -> None:
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import AgentMessage

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
        rows = conn.execute("PRAGMA table_info(agent_messages)").fetchall()
    finally:
        conn.close()
    sqlite_cols = {r[1] for r in rows}
    assert sqlite_cols == model_cols, (
        f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
    )


def test_agent_message_model_nullability_matches_raw_schema(tmp_path) -> None:
    """Per-column NOT NULL flags must match between ORM and raw DDL."""
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import AgentMessage

    model_notnull = {
        c.name
        for c in AgentMessage.__table__.columns
        if not c.nullable and not c.primary_key
    }

    from agent_mcp.core.config import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    try:
        rows = conn.execute("PRAGMA table_info(agent_messages)").fetchall()
    finally:
        conn.close()
    sqlite_notnull = {r[1] for r in rows if r[3] == 1 and r[5] == 0}
    assert sqlite_notnull == model_notnull, (
        f"sqlite NOT NULL {sqlite_notnull} != ORM {model_notnull}"
    )
