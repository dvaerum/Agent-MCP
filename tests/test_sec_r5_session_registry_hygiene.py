"""AC-R5-2 [LOW, memory hygiene]: expire_stale must evict cached Principals.

`unregister_session` and `detach_runtime_queue` both pop from
`_runtime_principals` when they drop a session's runtime queue, but
`expire_stale` historically popped only `_runtime_queues`. A
stale-reaped session's cached :class:`Principal` therefore lingered in
`_runtime_principals` for the life of the process — unbounded growth as
sessions get reaped over time.

Not a security exploit (lookup is by the unique uuid4 `session_id`, so a
leaked Principal can never be reassociated with a new session), purely
memory hygiene. These tests pin the eviction contract: `expire_stale`
must drop the reaped id from BOTH in-memory maps, while leaving a live
(non-stale) session's cached Principal untouched.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from agent_mcp.core.principal import Principal
from tests.harness import make_principal, mcp_session, seed_agent_rows


pytestmark = pytest.mark.asyncio


def _agent_principal(agent_id: str) -> Principal:
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token="tok-" + agent_id,
    )


def _backdate(session_id: str, seconds: int) -> None:
    """Push a session's last_seen_at `seconds` into the past."""
    from agent_mcp.db.connection import get_db_connection

    long_ago = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)
    ).isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE mcp_sessions SET last_seen_at = ? WHERE session_id = ?",
            (long_ago, session_id),
        )
        conn.commit()
    finally:
        conn.close()


async def test_expire_stale_evicts_cached_principal(tmp_path) -> None:
    """A stale-reaped session's Principal is dropped from both maps."""
    from agent_mcp.core import session_registry as reg

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        stale = reg.register_session(agent_id="alice", bearer_token="t-stale")
        reg.attach_principal(stale, _agent_principal("alice"))

        # Sanity: both in-memory maps hold the session before reaping.
        assert reg.get_principal(stale) is not None
        assert stale in reg._runtime_principals

        _backdate(stale, 3600)

        deleted = reg.expire_stale(threshold_seconds=300)
        assert stale in deleted

        # The whole point of AC-R5-2: the reaped id is gone from BOTH
        # the runtime-queue map AND the principal cache.
        assert stale not in reg._runtime_queues
        assert stale not in reg._runtime_principals
        assert reg.get_principal(stale) is None


async def test_expire_stale_keeps_live_session_principal(tmp_path) -> None:
    """A live (non-stale) session's cached Principal survives the reap."""
    from agent_mcp.core import session_registry as reg

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        fresh = reg.register_session(agent_id="alice", bearer_token="t-fresh")
        stale = reg.register_session(agent_id="alice", bearer_token="t-stale")
        reg.attach_principal(fresh, _agent_principal("alice"))
        reg.attach_principal(stale, _agent_principal("alice"))

        _backdate(stale, 3600)

        deleted = reg.expire_stale(threshold_seconds=300)
        assert stale in deleted
        assert fresh not in deleted

        # The live session's Principal is untouched...
        assert reg.get_principal(fresh) is not None
        assert fresh in reg._runtime_principals
        # ...while the stale one is evicted.
        assert reg.get_principal(stale) is None
