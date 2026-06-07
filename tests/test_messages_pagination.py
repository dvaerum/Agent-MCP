"""Backend offset/pagination contract regression for POST /api/messages/query.

Context: the dashboard's Messages tab is gaining an Older / Newer /
Newest / Oldest pagination footer (v5.0.26). The new UI sends `offset`
on every request and relies on the response's `total` field to render
"Showing 101–200 of 247". The backend has supported `limit`/`offset`
since Phase 6 PR #20, but no test pinned the contract — a regression
in that endpoint would silently break the new dashboard pagination.

These tests pass on `main` today (no backend change ships in this PR);
they exist as a regression guard for the contract the dashboard now
depends on.

Test shape: seed 150 `agent_messages` rows with deterministic timestamps
spaced one second apart (so the DESC ORDER BY is stable), then assert:

  * offset=0, limit=100 → 100 rows, total=150, first.timestamp > last.timestamp
  * offset=100, limit=100 → 50 rows, total=150
  * offset=150, limit=100 → 0 rows, total=150
  * offset=999, limit=100 → 0 rows, total=150 (graceful overshoot)

Wraps all 150 inserts in a single connection + single commit so the
seed is sub-second.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_many_messages(
    sender_id: str,
    recipient_id: str,
    count: int,
    *,
    base_time: _dt.datetime | None = None,
) -> None:
    """Insert ``count`` agent_messages rows with timestamps spaced one
    second apart (oldest first).

    Wraps all inserts in a single connection + single commit so the
    seed completes in well under a second even at count=150.
    """
    from agent_mcp.db.connection import get_db_connection

    if base_time is None:
        # Pin a deterministic start so timestamps don't collide with
        # any rows the lifespan may insert during setup.
        base_time = _dt.datetime(2026, 1, 1, 0, 0, 0)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        for i in range(count):
            ts = (base_time + _dt.timedelta(seconds=i)).isoformat()
            msg_id = f"msg_{secrets.token_hex(6)}"
            cursor.execute(
                "INSERT INTO agent_messages (message_id, sender_id, "
                "recipient_id, message_content, message_type, priority, "
                "timestamp, delivered, read) "
                "VALUES (?, ?, ?, ?, 'text', 'normal', ?, 0, 0)",
                (msg_id, sender_id, recipient_id, f"msg #{i}", ts),
            )
        conn.commit()
    finally:
        conn.close()


async def test_query_offset_first_page_returns_100_with_total_150(
    tmp_path,
) -> None:
    """offset=0 returns the newest 100 rows, total reflects the full
    150, and the page is sorted newest-first (DESC by timestamp)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("A")
        await admin.create_worker("B")
        _seed_many_messages("A", "B", 150)

        r = admin.client.post(
            "/api/messages/query",
            json={
                "token": admin.admin_token,
                "from": "A",
                "to": "B",
                "limit": 100,
                "offset": 0,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        rows = body["messages"]
        assert len(rows) == 100, f"expected 100 rows, got {len(rows)}"
        assert body["total"] == 150, body
        # DESC by timestamp — first row is strictly newer than last.
        assert rows[0]["timestamp"] > rows[-1]["timestamp"], (
            f"expected DESC order, got first={rows[0]['timestamp']!r} "
            f"last={rows[-1]['timestamp']!r}"
        )


async def test_query_offset_100_returns_remaining_50(tmp_path) -> None:
    """offset=100 with limit=100 returns the remaining 50 rows."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("A")
        await admin.create_worker("B")
        _seed_many_messages("A", "B", 150)

        r = admin.client.post(
            "/api/messages/query",
            json={
                "token": admin.admin_token,
                "from": "A",
                "to": "B",
                "limit": 100,
                "offset": 100,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["messages"]) == 50, body
        assert body["total"] == 150, body


async def test_query_offset_at_total_returns_zero_rows(tmp_path) -> None:
    """offset=150 (exactly at total) returns an empty page but still
    reports total=150 so the UI can render "no more messages"."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("A")
        await admin.create_worker("B")
        _seed_many_messages("A", "B", 150)

        r = admin.client.post(
            "/api/messages/query",
            json={
                "token": admin.admin_token,
                "from": "A",
                "to": "B",
                "limit": 100,
                "offset": 150,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["messages"] == [], body
        assert body["total"] == 150, body


async def test_query_offset_overshoot_is_graceful(tmp_path) -> None:
    """offset=999 is well past total; endpoint must respond 200 with
    an empty page (not 400, not 500). Guards against the dashboard
    racing a stale total."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("A")
        await admin.create_worker("B")
        _seed_many_messages("A", "B", 150)

        r = admin.client.post(
            "/api/messages/query",
            json={
                "token": admin.admin_token,
                "from": "A",
                "to": "B",
                "limit": 100,
                "offset": 999,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["messages"] == [], body
        assert body["total"] == 150, body
