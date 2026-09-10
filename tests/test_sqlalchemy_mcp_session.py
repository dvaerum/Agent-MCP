"""ORM model + parity test for the `mcp_sessions` table (db-review PR-G5).

Fifth model in the incremental SQLAlchemy adoption (after
`ProjectContext`, `Agent`, `Task`, `AgentMessage`).

Special to this table:

* PR-G1 (migration 0008) declared `mcp_sessions.agent_id ->
  agents.agent_id` as a NOT NULL FK. Tests that insert rows must
  ensure the agent exists first.
* The `agent` SQLAlchemy `relationship()` lets the ORM eagerly load
  the parent Agent -- exercised here as a sanity check.

Phase F (prancy-napping-pie): the session-registry cutover tests this
file used to carry (`register_session`/`touch_session`/etc., exercised
via `tests.harness.mcp_session`) were deleted along with
`agent_mcp.core.session_registry` itself -- the Rust port's own
session lifecycle (`rmcp`'s `LocalSessionManager`) has no Python
equivalent to keep testing. What remains here is pure ORM/schema-
parity coverage, rewritten to boot the DB layer directly instead of
the now-retired full app stack.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
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


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _seed_agent(session, agent_id: str) -> None:
    from agent_mcp.db.models import Agent

    session.add(
        Agent(
            token=f"tok-{agent_id}",
            agent_id=agent_id,
            created_at=_now_utc_iso(),
            status="active",
            working_directory="/tmp",
        )
    )


def test_mcp_session_model_round_trip(tmp_path) -> None:
    """ORM model can write a row and read it back identically."""
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import McpSession

    now = _now_utc_iso()
    with get_session() as session:
        _seed_agent(session, "alice")
        session.commit()

    with get_session() as session:
        row = McpSession(
            session_id="orm-round-trip-sid",
            agent_id="alice",
            opened_at=now,
            last_seen_at=now,
            bearer_token_hash=_hash("tok-alice"),
            alias_used=None,
        )
        session.add(row)
        session.commit()

    with get_session() as session:
        fetched = (
            session.query(McpSession)
            .filter(McpSession.session_id == "orm-round-trip-sid")
            .one_or_none()
        )
        assert fetched is not None
        assert fetched.agent_id == "alice"
        assert fetched.bearer_token_hash == _hash("tok-alice")
        assert fetched.alias_used is None


def test_mcp_session_model_columns_match_raw_schema(tmp_path) -> None:
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import McpSession

    model_cols = {c.name for c in McpSession.__table__.columns}
    assert model_cols == {
        "session_id",
        "agent_id",
        "opened_at",
        "last_seen_at",
        "bearer_token_hash",
        "alias_used",
    }, f"ORM columns drifted from raw schema: {model_cols}"

    from agent_mcp.core.config import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    try:
        rows = conn.execute("PRAGMA table_info(mcp_sessions)").fetchall()
    finally:
        conn.close()
    sqlite_cols = {r[1] for r in rows}
    assert sqlite_cols == model_cols, (
        f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
    )


def test_mcp_session_model_nullability_matches_raw_schema(tmp_path) -> None:
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.models import McpSession

    model_notnull = {
        c.name
        for c in McpSession.__table__.columns
        if not c.nullable and not c.primary_key
    }

    from agent_mcp.core.config import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    try:
        rows = conn.execute("PRAGMA table_info(mcp_sessions)").fetchall()
    finally:
        conn.close()
    sqlite_notnull = {r[1] for r in rows if r[3] == 1 and r[5] == 0}
    assert sqlite_notnull == model_notnull, (
        f"sqlite NOT NULL {sqlite_notnull} != ORM {model_notnull}"
    )


def test_mcp_session_agent_relationship_loads_parent(tmp_path) -> None:
    """The `agent` relationship resolves to the matching Agent row.

    Confirms the model's `relationship()` declaration is wired
    correctly -- the FK constraint is in place (PR-G1) and the ORM
    can navigate it without an explicit JOIN.
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import McpSession

    now = _now_utc_iso()
    with get_session() as session:
        _seed_agent(session, "alice")
        session.commit()

    with get_session() as session:
        session.add(
            McpSession(
                session_id="rel-sid",
                agent_id="alice",
                opened_at=now,
                last_seen_at=now,
                bearer_token_hash=_hash("tok-alice"),
            )
        )
        session.commit()

    with get_session() as session:
        row = (
            session.query(McpSession)
            .filter(McpSession.session_id == "rel-sid")
            .one()
        )
        agent = row.agent
        assert agent is not None
        assert agent.agent_id == "alice"
