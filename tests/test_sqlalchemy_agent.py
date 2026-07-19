"""ORM model + parity test for the `agents` table (db-review PR-G2).

Second model in the incremental SQLAlchemy adoption that started
with `ProjectContext`. The model must mirror what
`init_database()` creates for fresh DBs; this test catches drift
and pins the read-side cutover of `agent_mcp.repositories.agent_repository`.

Mirrors the shape of `tests/test_sqlalchemy_project_context.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_agent_model_round_trip(tmp_path) -> None:
    """ORM model can write a row and read it back identically."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    async with mcp_session(tmp_path):
        now = _dt.datetime.now().isoformat()
        with get_session() as session:
            row = Agent(
                token="tok-round-trip",
                agent_id="orm_round_trip",
                capabilities=json.dumps(["cap1", "cap2"]),
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
            assert json.loads(fetched.capabilities) == ["cap1", "cap2"]
            assert fetched.status == "active"
            assert fetched.working_directory == "/tmp"
            assert fetched.color == "#abc123"
            assert fetched.created_at == now


async def test_agent_model_columns_match_raw_schema(tmp_path) -> None:
    """ORM model columns must match the raw SQL schema exactly.

    If `init_database()` ever drifts from the model (or a migration
    adds a column without updating the model), this catches it.
    """
    from agent_mcp.db.models import Agent

    async with mcp_session(tmp_path):
        model_cols = {c.name for c in Agent.__table__.columns}
        assert model_cols == {
            "token",
            "agent_id",
            "capabilities",
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


async def test_agent_model_nullability_matches_raw_schema(tmp_path) -> None:
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
    from agent_mcp.db.models import Agent

    async with mcp_session(tmp_path):
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


async def test_agent_db_get_agent_by_id_uses_orm(tmp_path) -> None:
    """`get_agent_by_id` returns the same dict shape after the ORM cutover.

    The function is consumed by tool authorisation; the dict shape is
    a soft contract — every callsite indexes by string key (e.g.
    `agent['status']`, `agent['agent_id']`). The ORM cutover must
    preserve that.
    """
    from agent_mcp.repositories.agent_repository import get_agent_by_id

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        agent = get_agent_by_id("alice")
        assert agent is not None
        assert agent["agent_id"] == "alice"
        assert agent["status"] == "active"
        # capabilities must be deserialised to a Python list (the raw
        # column stores JSON-as-text).
        assert isinstance(agent["capabilities"], list)


async def test_agent_db_get_agent_by_token_uses_orm(tmp_path) -> None:
    from agent_mcp.repositories.agent_repository import get_agent_by_token

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        agent = get_agent_by_token(alice.token)
        assert agent is not None
        assert agent["agent_id"] == "alice"
        assert isinstance(agent["capabilities"], list)


async def test_agent_db_get_agent_by_id_missing_returns_none(tmp_path) -> None:
    from agent_mcp.repositories.agent_repository import get_agent_by_id

    async with mcp_session(tmp_path):
        assert get_agent_by_id("no-such-agent") is None


async def test_agent_db_get_all_active_agents_uses_orm(tmp_path) -> None:
    """`get_all_active_agents_from_db` excludes terminated rows and
    returns dicts compatible with `g.active_agents` consumers."""
    from agent_mcp.repositories.agent_repository import get_all_active_agents_from_db

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        rows = get_all_active_agents_from_db()
        ids = {r["agent_id"] for r in rows}
        # admin pseudo-agent (PR-G1) + alice + bob — all non-terminated.
        assert {"alice", "bob"}.issubset(ids)
        # Capabilities must be a list (JSON-decoded).
        for r in rows:
            assert isinstance(r["capabilities"], list)


async def test_agent_db_update_agent_field_uses_orm(tmp_path) -> None:
    """`update_agent_db_field` mutates the row via the ORM and bumps
    updated_at."""
    from agent_mcp.repositories.agent_repository import (
        get_agent_by_id,
        update_agent_db_field,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        before = get_agent_by_id("alice")
        assert before is not None
        assert before["status"] == "active"

        ok = update_agent_db_field("alice", "status", "terminated")
        assert ok is True

        after = get_agent_by_id("alice")
        assert after is not None
        assert after["status"] == "terminated"
        # updated_at should have advanced (or at least be set).
        assert after["updated_at"] is not None


async def test_agent_db_update_agent_field_capabilities_serialises(
    tmp_path,
) -> None:
    """Updating `capabilities` must JSON-serialise the list."""
    from agent_mcp.repositories.agent_repository import (
        get_agent_by_id,
        update_agent_db_field,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        ok = update_agent_db_field("alice", "capabilities", ["a", "b"])
        assert ok is True

        after = get_agent_by_id("alice")
        assert after is not None
        assert after["capabilities"] == ["a", "b"]


async def test_agent_db_update_agent_field_rejects_unknown_field(
    tmp_path,
) -> None:
    """Unsupported field names must be rejected (anti-injection guard)."""
    from agent_mcp.repositories.agent_repository import update_agent_db_field

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        ok = update_agent_db_field("alice", "; DROP TABLE agents; --", "x")
        assert ok is False
