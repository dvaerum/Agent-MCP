"""Regression guard: POST /api/messages/participants must exclude
``status='tombstone'`` rows from the ``live`` list.

Live-verified on washing-brothers production (2026-06-06): the endpoint
returned 9 ``live`` entries, 6 of which were ``[deleted-...]`` tombstone
rows with ``status='tombstone'``. These rows are FK-targets written by
the purge cascade (``_purge_tombstone`` in ``agent_mcp/app/routes.py``)
to satisfy the ``agent_messages.{sender_id, recipient_id}`` FK after a
hard purge. They are DB-internal placeholders — NOT a real "live" agent
state — and must not leak into the participants dropdown.

The original filter ``WHERE status IS NULL OR status != 'terminated'``
let ``status='tombstone'`` through. The fix tightens the predicate to
also exclude tombstones; we additionally pin the existing
non-terminated-and-terminated behaviour as a regression net.

Parallels ``test_purge_drops_visible_count.py``'s
``test_all_data_excludes_tombstone_rows`` / ``test_agents_list_excludes
_tombstone_rows`` which already guard ``/api/all-data`` and
``/api/agents``; this test extends the same contract to the
participants endpoint.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _insert_tombstone(agent_id: str) -> None:
    """Mirror the purge cascade's INSERT OR IGNORE of the tombstone row.

    Copied verbatim from ``test_purge_drops_visible_count.py`` so the
    seeding shape is consistent across the tombstone-leak regression
    suite.
    """
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO agents "
            "(token, agent_id, created_at, status, "
            " working_directory, color, updated_at) "
            "VALUES (?, ?, ?, 'tombstone', '', '#000000', ?)",
            (
                f"__tombstone_{agent_id}",
                f"[deleted-{agent_id}]",
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def _seed_terminated_worker(agent_id: str) -> None:
    """Insert a worker row in the terminated state (mirrors the helper
    in ``test_rest_messages_endpoints.py``)."""
    from agent_mcp.db.connection import get_db_connection

    token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token, agent_id, now, "terminated", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()


async def test_participants_live_excludes_tombstones(tmp_path) -> None:
    """The bug: rows with ``status='tombstone'`` leaked into ``live``
    because the original SQL only filtered ``!= 'terminated'``."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _insert_tombstone("ghost-1")
        _insert_tombstone("ghost-2")

        r = admin.post(
            "/api/messages/participants",
            json={},
        )
        assert r.status_code == 200, r.text
        body = r.json()

        live = body.get("live", [])
        live_ids = [a["agent_id"] for a in live]
        live_statuses = [a.get("status") for a in live]

        # Tombstone agent_ids must NOT appear in live.
        assert "[deleted-ghost-1]" not in live_ids, (
            f"tombstone row leaked into participants.live: {live_ids}"
        )
        assert "[deleted-ghost-2]" not in live_ids, (
            f"tombstone row leaked into participants.live: {live_ids}"
        )
        # And no row with status='tombstone' may appear, regardless of
        # agent_id shape. Status-based predicate is the canonical fix.
        assert "tombstone" not in live_statuses, (
            f"row with status='tombstone' leaked into participants.live: "
            f"{list(zip(live_ids, live_statuses))}"
        )


async def test_participants_live_includes_non_terminated_agents(
    tmp_path,
) -> None:
    """Regression net: real workers (status in ('created','running','
    pending','failed', NULL, ...)) MUST stay in ``live``."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        _insert_tombstone("ghost-3")

        r = admin.post(
            "/api/messages/participants",
            json={},
        )
        assert r.status_code == 200, r.text
        live_ids = [a["agent_id"] for a in r.json().get("live", [])]

        assert "alice" in live_ids, live_ids
        assert "bob" in live_ids, live_ids


async def test_participants_live_still_excludes_terminated(tmp_path) -> None:
    """Regression net: pre-existing terminated-agent filtering must
    keep working. We don't want the fix to accidentally widen the
    predicate."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _seed_terminated_worker("bob-terminated")

        r = admin.post(
            "/api/messages/participants",
            json={},
        )
        assert r.status_code == 200, r.text
        live_ids = [a["agent_id"] for a in r.json().get("live", [])]

        assert "alice" in live_ids, live_ids
        assert "bob-terminated" not in live_ids, (
            f"terminated agent leaked back into participants.live: "
            f"{live_ids}"
        )
