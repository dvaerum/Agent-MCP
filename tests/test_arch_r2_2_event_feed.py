"""arch-deepening round-2 #2 — one ``assemble_event_feed`` owner + the
fold-in fix pinned below.

**No self-wake on a no-op cursor advance**: ``advance_event_cursor``
published ``agent.updated`` UNCONDITIONALLY on every cursor write.
``agent.updated`` fans out to ``state.notify_waiters`` → every
sibling ``wait_for_events`` waiter for that agent wakes and re-queries
for nothing. Under fan-out (N concurrent waiters each writing the same
high-water cursor) that is O(N) spurious wakes per event round. A
no-op advance (cursor <= current) must fire NO wake at all.

A sibling invariant (inbox ≡ wait_for_events for the same cursor) was
also pinned here until Phase F's ``agent_mcp/resources/`` deletion
retired the inbox resource; the Rust port's
`resources.rs::render_inbox` already routes through the same
`assemble_event_feed` pipeline `wait_for_events`/`fetch_events_since`
use (see that module's own doc), so the divergence this test guarded
against is structurally unrepresentable there.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


_BASE = _dt.datetime(2026, 1, 1, 0, 0, 0)


def _ts(i: int) -> str:
    return (_BASE + _dt.timedelta(seconds=i)).isoformat()


async def test_noop_cursor_advance_does_not_publish_agent_updated(
    tmp_path: Path,
) -> None:
    """A cursor advance that does NOT change ``last_event_seen_at``
    (equal or lower value) must not publish ``agent.updated`` — that
    event wakes every sibling ``wait_for_events`` waiter for nothing.
    RED on main (publishes unconditionally)."""
    import agent_mcp.repositories.agent_repository as agent_repository
    from agent_mcp.repositories import agent_repo
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        published: list[tuple[str, str]] = []
        orig_publish = agent_repository._publish

        def _spy(addressee, event, payload):
            published.append((event, (payload or {}).get("field")))
            return orig_publish(addressee, event, payload)

        agent_repository._publish = _spy
        try:
            # First advance: NULL -> _ts(10) is a REAL change; it may
            # publish (that's fine — a genuine state change).
            assert agent_repo.advance_event_cursor("alice", _ts(10)) is True
            published.clear()

            # No-op advances: equal, then lower. MAX semantics keep the
            # column at _ts(10), so nothing changed → nothing published.
            assert agent_repo.advance_event_cursor("alice", _ts(10)) is True
            assert agent_repo.advance_event_cursor("alice", _ts(5)) is True
        finally:
            agent_repository._publish = orig_publish

        cursor_updates = [
            (evt, field)
            for (evt, field) in published
            if evt == "agent.updated" and field == "last_event_seen_at"
        ]
        assert cursor_updates == [], (
            "a no-op cursor advance must NOT publish agent.updated "
            f"(would self-wake every sibling waiter); got {cursor_updates}"
        )


async def test_real_cursor_advance_still_persists_monotonically(
    tmp_path: Path,
) -> None:
    """The self-wake fix must not weaken the MAX-based monotonic advance:
    a higher cursor moves the watermark, a lower one never rewinds it."""
    from agent_mcp.repositories import agent_repo
    from agent_mcp.tools.agent_communication_tools import (
        _read_last_event_seen_at,
    )
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        assert agent_repo.advance_event_cursor("alice", _ts(10)) is True
        assert _read_last_event_seen_at("alice") == _ts(10)

        # Lower value must not rewind.
        assert agent_repo.advance_event_cursor("alice", _ts(3)) is True
        assert _read_last_event_seen_at("alice") == _ts(10)

        # Higher value advances.
        assert agent_repo.advance_event_cursor("alice", _ts(20)) is True
        assert _read_last_event_seen_at("alice") == _ts(20)
