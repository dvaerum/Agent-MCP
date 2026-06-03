"""Test suite for PR-3 of the database review improvements.

Covers items 2, 9, and 10 from the 2026-06-02 review:

  * Item 2 — bound `/api/all-data` with a default LIMIT (+ optional
    `?limit=` override) so a project with thousands of agents/tasks
    no longer ships an unbounded blob on every dashboard refresh.
  * Item 9 — replace the O(n²) token-matching loop in `/api/all-data`
    with a dict lookup keyed by agent_id (already pre-built once).
    Behavior assertion: every agent still receives an auth_token if
    the corresponding active_agents entry exists.
  * Item 10 — `get_agent_messages` no longer scans the result set in
    Python to decide which message IDs to mark as read; the mark
    becomes a single SQL UPDATE keyed on `recipient_id = ? AND read =
    0`.

The tests are behavior-focused: we don't pin "exactly one UPDATE
statement" because the test harness doesn't introspect cursor calls;
we pin the observable effects (rows are marked read, the response
shape is unchanged, the LIMIT trims as expected).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Item 2 — bound /api/all-data
# ---------------------------------------------------------------------------


async def test_all_data_applies_default_limit_per_section(tmp_path) -> None:
    """Default response trims to <= the documented per-section cap.

    We seed more tasks than the default cap and confirm the response
    only carries `cap` of them.
    """
    DEFAULT_LIMIT = 500  # must match the constant in routes.py
    OVERSHOOT = 5
    async with mcp_session(tmp_path) as admin:
        # Seed DEFAULT_LIMIT + OVERSHOOT tasks. They all show up in the
        # tasks table; we only care that the API trims at the cap.
        for i in range(DEFAULT_LIMIT + OVERSHOOT):
            admin.client.post(
                "/api/tasks",
                json={
                    "token": admin.admin_token,
                    "task_title": f"seed-{i}",
                    "task_description": "limit test",
                },
            )

        resp = admin.client.get("/api/all-data")
        assert resp.status_code == 200
        body = resp.json()
        assert "tasks" in body
        assert len(body["tasks"]) == DEFAULT_LIMIT, (
            f"expected {DEFAULT_LIMIT} tasks (default limit), "
            f"got {len(body['tasks'])}"
        )


async def test_all_data_accepts_limit_query_param(tmp_path) -> None:
    """`?limit=N` overrides the default per-section cap."""
    async with mcp_session(tmp_path) as admin:
        for i in range(10):
            r = admin.client.post(
                "/api/tasks",
                json={
                    "token": admin.admin_token,
                    "task_title": f"limit-override-{i}",
                    "task_description": "x",
                },
            )
            assert r.status_code == 200, r.text

        resp = admin.client.get("/api/all-data?limit=3")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["tasks"]) == 3


async def test_all_data_limit_clamped_to_safe_range(tmp_path) -> None:
    """Crazy `?limit=` values clamp to the safe range.

    Pinning this stops a future "limit=10000000" from materialising
    an unbounded list and re-introducing the original problem.
    """
    MAX_LIMIT = 5000  # must match the constant in routes.py
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.get(f"/api/all-data?limit={MAX_LIMIT * 10}")
        assert resp.status_code == 200
        # No way to verify the exact applied limit without seeding
        # MAX_LIMIT * 10 + 1 rows; rely on absence of error + body
        # parses + tasks list is bounded. (We seeded zero, so the
        # list is empty; the success here is "no 500 from running
        # the clamped query".)
        body = resp.json()
        assert isinstance(body["tasks"], list)

        # Negative limits should also be clamped.
        resp = admin.client.get("/api/all-data?limit=-7")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Item 9 — O(n²) token lookup -> dict
# ---------------------------------------------------------------------------


async def test_all_data_attaches_auth_token_to_every_known_agent(
    tmp_path,
) -> None:
    """The token lookup must still attach an auth_token to each agent.

    The dict-based optimization mustn't drop the side effect — we
    verify it for at least one created worker.
    """
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        resp = admin.client.get("/api/all-data")
        assert resp.status_code == 200
        agents = resp.json()["agents"]

        by_id = {a["agent_id"]: a for a in agents}
        # Admin is injected; alice + bob were created.
        assert "alice" in by_id
        assert "bob" in by_id
        # Both should have an auth_token attached from the in-memory
        # active_agents lookup.
        assert by_id["alice"].get("auth_token"), (
            f"alice has no auth_token after dict-lookup refactor: "
            f"{by_id['alice']}"
        )
        assert by_id["bob"].get("auth_token")


# ---------------------------------------------------------------------------
# Item 10 — mark_as_read becomes a single UPDATE on (recipient, read)
# ---------------------------------------------------------------------------


async def test_get_agent_messages_marks_all_received_unread(tmp_path) -> None:
    """mark_as_read=True must mark **every** received-unread message,
    not just the ones returned by the LIMIT-bounded SELECT.

    The refactor changes mark_as_read from "filter the fetched rows,
    UPDATE by IDs" (which leaves unread rows beyond the limit
    flagged) to a single UPDATE `WHERE recipient_id=? AND read=0`
    that covers them all. We seed more unread than the request's
    limit so the pre-refactor behavior would visibly differ from the
    refactored behavior.
    """
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        # Seed three unread messages directly via SQL — the
        # send_agent_message MCP tool is worker-to-worker-policy-gated
        # and turning that policy on would muddy this test's focus
        # (which is purely the mark-as-read SQL path).
        import datetime as _dt

        # Seed 30 unread so that with a request limit of 5 the
        # pre-refactor mark_as_read would only flag the 5 fetched
        # messages; the refactored behavior must flag all 30.
        conn = get_db_connection()
        try:
            for i in range(30):
                conn.execute(
                    "INSERT INTO agent_messages "
                    "(message_id, sender_id, recipient_id, "
                    " message_content, message_type, priority, "
                    " timestamp, delivered, read) "
                    "VALUES (?, 'admin', 'alice', ?, 'text', 'normal', "
                    "        ?, 1, 0)",
                    (
                        f"seed-{i}",
                        f"hello {i}",
                        _dt.datetime.now().isoformat(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        # Sanity: all 30 are unread for alice.
        conn = get_db_connection()
        try:
            unread = conn.execute(
                "SELECT COUNT(*) FROM agent_messages "
                "WHERE recipient_id = 'alice' AND read = 0"
            ).fetchone()[0]
        finally:
            conn.close()
        assert unread == 30, f"expected 30 unread for alice, got {unread}"

        # Have alice fetch with a tight limit so the SELECT only sees
        # 5 of the 30 unread. After the refactor, mark_as_read must
        # still clear all 30.
        await alice.call(
            "get_agent_messages",
            {"token": alice.token, "limit": 5},
        )

        # All received unread should now be 0.
        conn = get_db_connection()
        try:
            still_unread = conn.execute(
                "SELECT COUNT(*) FROM agent_messages "
                "WHERE recipient_id = 'alice' AND read = 0"
            ).fetchone()[0]
        finally:
            conn.close()
        assert still_unread == 0, (
            f"mark_as_read should have cleared all received unread "
            f"for alice; {still_unread} still flagged unread"
        )


async def test_get_agent_messages_mark_as_read_false_keeps_unread(
    tmp_path,
) -> None:
    """Passing mark_as_read=False leaves the unread flags intact."""
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        import datetime as _dt

        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO agent_messages "
                "(message_id, sender_id, recipient_id, "
                " message_content, message_type, priority, "
                " timestamp, delivered, read) "
                "VALUES ('keep-unread', 'admin', 'alice', "
                "        'do not mark', 'text', 'normal', ?, 1, 0)",
                (_dt.datetime.now().isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()

        await alice.call(
            "get_agent_messages",
            {"token": alice.token, "limit": 5, "mark_as_read": False},
        )

        conn = get_db_connection()
        try:
            unread = conn.execute(
                "SELECT COUNT(*) FROM agent_messages "
                "WHERE recipient_id = 'alice' AND read = 0"
            ).fetchone()[0]
        finally:
            conn.close()
        assert unread == 1, (
            f"mark_as_read=False should preserve unread flag; got "
            f"unread={unread}"
        )
