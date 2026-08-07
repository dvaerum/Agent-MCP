"""ORM model + parity test for the `mcp_sessions` table (db-review PR-G5).

Fifth model in the incremental SQLAlchemy adoption (after
`ProjectContext`, `Agent`, `Task`, `AgentMessage`). Mirrors the
established pattern from `tests/test_sqlalchemy_agent_message.py`.

Special to this table:

* PR-G1 (migration 0008) declared `mcp_sessions.agent_id ->
  agents.agent_id` as a NOT NULL FK. Tests that insert rows must
  ensure the agent exists first.
* The `agent` SQLAlchemy `relationship()` lets the ORM eagerly load
  the parent Agent — exercised here as a sanity check.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import sqlite3
import time

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def test_mcp_session_model_round_trip(tmp_path) -> None:
    """ORM model can write a row and read it back identically."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import McpSession

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        now = _now_utc_iso()
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


async def test_mcp_session_model_columns_match_raw_schema(tmp_path) -> None:
    from agent_mcp.db.models import McpSession

    async with mcp_session(tmp_path):
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
            rows = conn.execute(
                "PRAGMA table_info(mcp_sessions)"
            ).fetchall()
        finally:
            conn.close()
        sqlite_cols = {r[1] for r in rows}
        assert sqlite_cols == model_cols, (
            f"sqlite schema {sqlite_cols} != ORM model {model_cols}"
        )


async def test_mcp_session_model_nullability_matches_raw_schema(
    tmp_path,
) -> None:
    from agent_mcp.db.models import McpSession

    async with mcp_session(tmp_path):
        model_notnull = {
            c.name
            for c in McpSession.__table__.columns
            if not c.nullable and not c.primary_key
        }

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            rows = conn.execute(
                "PRAGMA table_info(mcp_sessions)"
            ).fetchall()
        finally:
            conn.close()
        sqlite_notnull = {r[1] for r in rows if r[3] == 1 and r[5] == 0}
        assert sqlite_notnull == model_notnull, (
            f"sqlite NOT NULL {sqlite_notnull} != ORM {model_notnull}"
        )


async def test_mcp_session_agent_relationship_loads_parent(tmp_path) -> None:
    """The `agent` relationship resolves to the matching Agent row.

    Confirms the model's `relationship()` declaration is wired
    correctly — the FK constraint is in place (PR-G1) and the ORM
    can navigate it without an explicit JOIN.
    """
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import McpSession

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        now = _now_utc_iso()
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


async def test_register_session_uses_orm(tmp_path) -> None:
    from agent_mcp.core import session_registry
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import McpSession

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        sid = session_registry.register_session(
            agent_id="alice", bearer_token="bearer-alice",
        )
        assert sid and len(sid) == 32  # uuid4 hex

        with get_session() as s:
            row = (
                s.query(McpSession)
                .filter(McpSession.session_id == sid)
                .one()
            )
            assert row.agent_id == "alice"
            assert row.bearer_token_hash == _hash("bearer-alice")


async def test_register_session_records_alias_used(tmp_path) -> None:
    from agent_mcp.core import session_registry
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import McpSession

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        sid = session_registry.register_session(
            agent_id="alice",
            bearer_token="bearer-alice",
            alias_used="alice-alias-1",
        )

        with get_session() as s:
            row = (
                s.query(McpSession)
                .filter(McpSession.session_id == sid)
                .one()
            )
            assert row.alias_used == "alice-alias-1"


async def test_touch_session_updates_last_seen_at(tmp_path) -> None:
    from agent_mcp.core import session_registry
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import McpSession

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        sid = session_registry.register_session(
            agent_id="alice", bearer_token="bearer-alice",
        )

        with get_session() as s:
            before = s.query(McpSession).filter(
                McpSession.session_id == sid
            ).one().last_seen_at

        # Sleep enough that the ISO-8601 string definitely differs.
        await asyncio.sleep(0.01)
        session_registry.touch_session(sid)

        with get_session() as s:
            after = s.query(McpSession).filter(
                McpSession.session_id == sid
            ).one().last_seen_at
        assert after > before


async def test_unregister_session_deletes_row(tmp_path) -> None:
    from agent_mcp.core import session_registry
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import McpSession

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        sid = session_registry.register_session(
            agent_id="alice", bearer_token="bearer-alice",
        )

        session_registry.unregister_session(sid)

        with get_session() as s:
            row = (
                s.query(McpSession)
                .filter(McpSession.session_id == sid)
                .one_or_none()
            )
            assert row is None


async def test_unregister_session_missing_row_is_noop(tmp_path) -> None:
    """`unregister_session` must not raise when the row is gone —
    duplicate cleanup paths rely on this contract."""
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path):
        # No row inserted; must be a silent no-op.
        session_registry.unregister_session("no-such-sid")


async def test_sessions_for_agent_and_all_sessions(tmp_path) -> None:
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        sid_a1 = session_registry.register_session(
            agent_id="alice", bearer_token="bearer-alice-1",
        )
        sid_a2 = session_registry.register_session(
            agent_id="alice", bearer_token="bearer-alice-2",
        )
        sid_b = session_registry.register_session(
            agent_id="bob", bearer_token="bearer-bob",
        )

        for_alice = session_registry.sessions_for_agent("alice")
        alice_ids = {h.session_id for h in for_alice}
        assert alice_ids == {sid_a1, sid_a2}

        for_bob = session_registry.sessions_for_agent("bob")
        assert {h.session_id for h in for_bob} == {sid_b}

        all_ids = {h.session_id for h in session_registry.all_sessions()}
        assert {sid_a1, sid_a2, sid_b}.issubset(all_ids)


async def test_expire_stale_deletes_old_rows(tmp_path) -> None:
    from agent_mcp.core import session_registry
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        sid_old = session_registry.register_session(
            agent_id="alice", bearer_token="bearer-alice",
        )
        sid_fresh = session_registry.register_session(
            agent_id="alice", bearer_token="bearer-alice-2",
        )

        # Backdate the "old" session's last_seen_at by an hour.
        backdated = (
            _dt.datetime.now(_dt.UTC)
            - _dt.timedelta(hours=1)
        ).isoformat()
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE mcp_sessions SET last_seen_at = ? "
                "WHERE session_id = ?",
                (backdated, sid_old),
            )
            conn.commit()
        finally:
            conn.close()

        expired = session_registry.expire_stale(threshold_seconds=300)
        assert sid_old in expired
        assert sid_fresh not in expired

        remaining = {
            h.session_id for h in session_registry.all_sessions()
        }
        assert sid_old not in remaining
        assert sid_fresh in remaining


# `time` only imported to suppress the "unused" lint above when this
# file is imported but not all paths exercise it.
_ = time
