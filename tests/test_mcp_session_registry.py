"""Tests for the SQLite-backed MCP session registry (Phase: session-registry).

Why this exists
---------------

The current `signal_for(agent_id)` flow is in-process only — it wakes
`wait_for_events` tool callers running in the same Python process. That
is fine for tool-pulled state (`agent_messages`, task assignments)
because workers eventually poll via tool calls.

For MCP-protocol push notifications (`notifications/resources/updated`,
`notifications/tools/list_changed`) the emitter sits inside one
request's context and only reaches that request's session. In Streamable
HTTP stateless mode there is no in-memory enumeration of OTHER
in-flight `GET /mcp` streams, so a worker that opened a long-poll on
GET /mcp can't be notified by a different request's tool call.

The session registry is the discovery layer for that fan-out: every
open `GET /mcp` stream registers itself (session_id, agent_id, bearer
hash, last_seen_at) so emitters can enumerate subscribers and route
notifications to each. The registry is persisted in SQLite so the
*data* survives restarts; the in-memory queue of "where to actually
deliver" is rebuilt as streams reconnect.

These tests pin the registry module's API contract — the persistence
layer it sits on, the lifecycle (register / touch / unregister / expire
stale), and the per-session in-memory queue used by fan-out. They do
NOT exercise the StreamableHTTP transport wiring; that belongs in
follow-up integration tests once the GET /mcp lifecycle hook is in
place. Keeping this test file scoped to the registry alone means the
test suite stays fast and the registry's API can evolve independently
of the transport wiring.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib

import pytest


pytestmark = pytest.mark.usefixtures("client")  # ensures DB+lifespan are up


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_register_returns_session_id_and_persists_row(client) -> None:
    """register_session inserts a row and returns the minted session_id.

    The session_id must be a non-empty string (UUID under the hood),
    distinct per call, and the DB row must carry the agent_id +
    bearer-token hash (NEVER the raw token).
    """
    from agent_mcp.core import session_registry as reg
    from agent_mcp.db.connection import get_db_connection

    sid1 = reg.register_session(agent_id="alice", bearer_token="raw-token-1")
    sid2 = reg.register_session(agent_id="alice", bearer_token="raw-token-2")
    assert isinstance(sid1, str) and sid1
    assert sid1 != sid2

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, agent_id, bearer_token_hash "
            "FROM mcp_sessions WHERE session_id IN (?, ?) ORDER BY session_id",
            (sid1, sid2),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert len(rows) == 2
    by_sid = {r["session_id"]: r for r in rows}
    assert by_sid[sid1]["agent_id"] == "alice"
    assert by_sid[sid1]["bearer_token_hash"] == _sha("raw-token-1")
    # Belt-and-braces: raw token must not be stored anywhere on the row.
    assert "raw-token-1" not in str(by_sid[sid1])


def test_unregister_removes_row(client) -> None:
    from agent_mcp.core import session_registry as reg
    from agent_mcp.db.connection import get_db_connection

    sid = reg.register_session(agent_id="bob", bearer_token="tok-bob")
    reg.unregister_session(sid)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM mcp_sessions WHERE session_id = ?", (sid,))
        assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_unregister_unknown_session_is_noop(client) -> None:
    """Unregistering a session_id that doesn't exist must not raise.

    The transport-level hook calls unregister on every disconnect; a
    race or duplicate cleanup path shouldn't take the request down.
    """
    from agent_mcp.core import session_registry as reg

    reg.unregister_session("not-a-real-session-id")  # no exception


def test_touch_session_updates_last_seen(client) -> None:
    """`touch_session` bumps last_seen_at to "now" (monotonic per row)."""
    from agent_mcp.core import session_registry as reg
    from agent_mcp.db.connection import get_db_connection

    sid = reg.register_session(agent_id="carol", bearer_token="t")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_seen_at FROM mcp_sessions WHERE session_id = ?", (sid,),
        )
        before = cur.fetchone()["last_seen_at"]
    finally:
        conn.close()

    # Force a different timestamp by reaching past the registry's
    # internal clock — the simplest reliable way is to overwrite the
    # row to an ancient value and confirm touch_session moves it forward.
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE mcp_sessions SET last_seen_at = '2000-01-01T00:00:00+00:00' "
            "WHERE session_id = ?",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()

    reg.touch_session(sid)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_seen_at FROM mcp_sessions WHERE session_id = ?", (sid,),
        )
        after = cur.fetchone()["last_seen_at"]
    finally:
        conn.close()
    assert after != "2000-01-01T00:00:00+00:00"
    # Format sanity-check: ISO-8601 string parseable by datetime.
    _dt.datetime.fromisoformat(after)


def test_sessions_for_agent_returns_only_matching(client) -> None:
    from agent_mcp.core import session_registry as reg

    a1 = reg.register_session(agent_id="alice", bearer_token="t-a1")
    a2 = reg.register_session(agent_id="alice", bearer_token="t-a2")
    reg.register_session(agent_id="bob", bearer_token="t-b1")

    alice_sessions = reg.sessions_for_agent("alice")
    sids = {s.session_id for s in alice_sessions}
    assert sids == {a1, a2}
    # Every handle carries the agent_id back so callers don't re-query.
    assert all(s.agent_id == "alice" for s in alice_sessions)


def test_all_sessions_returns_every_row(client) -> None:
    from agent_mcp.core import session_registry as reg

    a = reg.register_session(agent_id="alice", bearer_token="t-a")
    b = reg.register_session(agent_id="bob", bearer_token="t-b")
    c = reg.register_session(agent_id="carol", bearer_token="t-c")
    sids = {s.session_id for s in reg.all_sessions()}
    assert {a, b, c}.issubset(sids)


def test_expire_stale_removes_old_sessions(client) -> None:
    """Rows with last_seen_at older than threshold get deleted."""
    from agent_mcp.core import session_registry as reg
    from agent_mcp.db.connection import get_db_connection

    fresh = reg.register_session(agent_id="alice", bearer_token="t-fresh")
    stale = reg.register_session(agent_id="alice", bearer_token="t-stale")

    # Backdate `stale` to a long time ago. ISO-UTC string with an
    # explicit timezone so the comparison in expire_stale is unambiguous.
    long_ago = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=3600)
    ).isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE mcp_sessions SET last_seen_at = ? WHERE session_id = ?",
            (long_ago, stale),
        )
        conn.commit()
    finally:
        conn.close()

    deleted = reg.expire_stale(threshold_seconds=300)
    assert stale in deleted
    assert fresh not in deleted

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id FROM mcp_sessions WHERE session_id IN (?, ?)",
            (fresh, stale),
        )
        remaining = {r["session_id"] for r in cur.fetchall()}
    finally:
        conn.close()
    assert remaining == {fresh}


# ---- in-memory queue tests --------------------------------------------------


def test_runtime_queue_attaches_and_detaches(client) -> None:
    """The runtime side stores per-session asyncio.Queues so fan-out
    emitters can push payloads keyed by session_id without re-reading
    the DB on every notification.

    Attach when a stream opens, detach on close. Detaching after the
    session is gone is a no-op (mirrors the unregister contract).
    """
    from agent_mcp.core import session_registry as reg

    sid = reg.register_session(agent_id="alice", bearer_token="t")
    q = asyncio.Queue()
    reg.attach_runtime_queue(sid, q)
    assert reg.get_runtime_queue(sid) is q
    reg.detach_runtime_queue(sid)
    assert reg.get_runtime_queue(sid) is None
    # Idempotent — second detach shouldn't raise.
    reg.detach_runtime_queue(sid)


def test_fanout_enqueues_to_every_attached_session_for_agent(client) -> None:
    """`fanout_to_agent(agent_id, payload)` puts `payload` on every
    runtime queue currently attached for that agent. Sessions without
    a queue (the DB row exists but no in-memory stream is bound) are
    silently skipped — they'll reconnect and pull the next event from
    the DB-backed source of truth.
    """
    from agent_mcp.core import session_registry as reg

    sid_a = reg.register_session(agent_id="alice", bearer_token="t-a")
    sid_b = reg.register_session(agent_id="alice", bearer_token="t-b")
    sid_c = reg.register_session(agent_id="bob", bearer_token="t-c")

    qa = asyncio.Queue()
    qb = asyncio.Queue()
    qc = asyncio.Queue()
    reg.attach_runtime_queue(sid_a, qa)
    reg.attach_runtime_queue(sid_b, qb)
    reg.attach_runtime_queue(sid_c, qc)

    payload = {"method": "notifications/resources/updated", "params": {}}
    delivered_to = reg.fanout_to_agent("alice", payload)
    assert {sid_a, sid_b} == set(delivered_to)
    assert qa.get_nowait() == payload
    assert qb.get_nowait() == payload
    assert qc.empty(), "bob's queue must not receive alice-scoped fanout"


def test_fanout_to_all_enqueues_to_every_attached_session(client) -> None:
    from agent_mcp.core import session_registry as reg

    sid_a = reg.register_session(agent_id="alice", bearer_token="t-a")
    sid_b = reg.register_session(agent_id="bob", bearer_token="t-b")
    qa = asyncio.Queue()
    qb = asyncio.Queue()
    reg.attach_runtime_queue(sid_a, qa)
    reg.attach_runtime_queue(sid_b, qb)

    payload = {"method": "notifications/tools/list_changed", "params": {}}
    delivered_to = reg.fanout_to_all(payload)
    assert {sid_a, sid_b}.issubset(set(delivered_to))
    assert qa.get_nowait() == payload
    assert qb.get_nowait() == payload
