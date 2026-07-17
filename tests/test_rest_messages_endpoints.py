"""REST endpoints for agent_messages (Phase 6 PR #20).

The dashboard's new Messages tab needs:
- GET /api/messages with filters (from/to/between/type/priority/
  read/since/until/q/limit/offset)
- POST /api/messages — admin compose
- PATCH /api/messages/<id> — flip read/delivered for admin housekeeping

Token in JSON body, matching the Q6a.1 convention used by the
existing memory + task endpoints.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). _seed_worker is replaced by
admin.create_worker (with a terminated-state helper for the
participants tests that need ghost agents).
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session, seed_agent_rows


pytestmark = pytest.mark.asyncio


async def _seed_terminated_worker(admin, name: str) -> None:
    """Insert a worker row in the terminated state. The harness's
    create_worker only knows about active workers; the participants
    endpoint tests need ghosts to assert they're filtered."""
    from agent_mcp.db.connection import get_db_connection

    token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, name, "[]", now, "terminated", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()
    # Do NOT register in g.active_agents — terminated workers aren't active.


def _seed_message_with_sender(
    sender_id: str, recipient_id: str = "alice", content: str = "x",
) -> str:
    """Insert an agent_messages row directly with an arbitrary sender.

    Used by the participants endpoint tests to plant tombstone rows
    (sender_id / recipient_id beginning with ``[deleted-``) that the
    REST compose path would otherwise refuse to create.
    """
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    msg_id = f"msg_{secrets.token_hex(6)}"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agent_messages (message_id, sender_id, recipient_id, "
        "message_content, message_type, priority, timestamp, "
        "delivered, read) "
        "VALUES (?, ?, ?, ?, 'text', 'normal', ?, 0, 0)",
        (msg_id, sender_id, recipient_id, content, now),
    )
    conn.commit()
    conn.close()
    return msg_id


# ---------- POST /api/messages ----------------------------------


async def test_post_messages_creates_message_with_admin_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "hello alice",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True, body
        assert "message_id" in body, body


async def test_post_messages_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.client.post(
            "/api/messages",
            # Fake bearer: exercises the operator-tier gate, not no-auth 401.
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={
                "recipient_id": "alice",
                "message_content": "hi",
            },
        )
        assert r.status_code == 401, r.text


async def test_post_messages_rejects_missing_recipient(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/messages",
            json={"message_content": "lonely"},
        )
        assert r.status_code == 400, r.text


async def test_post_messages_rejects_missing_content(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={"recipient_id": "alice"},
        )
        assert r.status_code == 400, r.text


# ---------- POST /api/messages/query (filtered listing) -------------


async def test_list_uses_post_query_not_get_with_body(tmp_path) -> None:
    """Regression: GET /api/messages returns 405. Filtered listing
    must be POST /api/messages/query because the Fetch spec strips
    bodies from GET requests in the browser.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.request(
            "GET", "/api/messages", json={}
        )
        assert r.status_code in (404, 405), (
            f"GET /api/messages should no longer be a route; "
            f"got {r.status_code}"
        )


# ---------- POST /api/messages/query ---------------------------


async def test_get_messages_lists_seeded_messages(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        # Seed via POST.
        posted = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "findable",
            },
        ).json()
        msg_id = posted["message_id"]

        r = admin.post(
            "/api/messages/query",
            json={},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "messages" in body
        ids = [m["message_id"] for m in body["messages"]]
        assert msg_id in ids


async def test_get_messages_filter_from(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "to alice from admin",
            },
        )

        r = admin.post(
            "/api/messages/query",
            json={"from": "admin"},
        )
        assert r.status_code == 200, r.text
        msgs = r.json()["messages"]
        assert all(m["sender_id"] == "admin" for m in msgs), (
            f"filter from=admin returned non-admin senders: "
            f"{[m['sender_id'] for m in msgs]}"
        )


async def test_get_messages_filter_to(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "to alice",
            },
        )
        admin.post(
            "/api/messages",
            json={
                "recipient_id": "bob",
                "message_content": "to bob",
            },
        )

        r = admin.post(
            "/api/messages/query",
            json={"to": "alice"},
        )
        msgs = r.json()["messages"]
        assert all(m["recipient_id"] == "alice" for m in msgs), (
            f"filter to=alice returned others: "
            f"{[m['recipient_id'] for m in msgs]}"
        )
        assert len(msgs) >= 1


async def test_get_messages_filter_read(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "unread one",
            },
        )

        # All seeded messages start with read=False.
        r = admin.post(
            "/api/messages/query",
            json={"read": False},
        )
        assert r.status_code == 200, r.text
        msgs = r.json()["messages"]
        assert all(m["read"] == 0 or m["read"] is False for m in msgs)
        assert len(msgs) >= 1


async def test_get_messages_filter_content_substring(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "pineapple pizza",
            },
        )
        admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "boring text",
            },
        )

        r = admin.post(
            "/api/messages/query",
            json={"q": "pineapple"},
        )
        msgs = r.json()["messages"]
        assert any("pineapple" in m["message_content"] for m in msgs)
        assert all("boring" not in m["message_content"] for m in msgs)


async def test_get_messages_pagination(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # Seed >2 messages.
        for i in range(3):
            admin.post(
                "/api/messages",
                json={
                    "recipient_id": "alice",
                    "message_content": f"msg {i}",
                },
            )

        r = admin.post(
            "/api/messages/query",
            json={"limit": 2, "offset": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["messages"]) <= 2


async def test_get_messages_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/messages/query",
            # Fake bearer: exercises the operator-tier gate, not no-auth 401.
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={},
        )
        assert r.status_code == 401, r.text


# ---------- PATCH /api/messages/<id> ----------------------------


async def test_patch_messages_marks_read(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        posted = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "to be read",
            },
        ).json()
        msg_id = posted["message_id"]

        r = admin.request(
            "PATCH",
            f"/api/messages/{msg_id}",
            json={"read": True},
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True

        # Verify via GET.
        listing = admin.post(
            "/api/messages/query",
            json={},
        ).json()
        msg = next(
            m for m in listing["messages"] if m["message_id"] == msg_id
        )
        assert msg["read"] in (1, True)


async def test_patch_messages_404_on_unknown_id(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "PATCH",
            "/api/messages/msg_doesnotexist",
            json={"read": True},
        )
        assert r.status_code == 404, r.text


async def test_patch_messages_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        posted = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "hi",
            },
        ).json()
        r = admin.client.request(
            "PATCH",
            f"/api/messages/{posted['message_id']}",
            # Fake bearer: exercises the operator-tier gate, not no-auth 401.
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={"read": True},
        )
        assert r.status_code == 401, r.text


# ---------- DELETE /api/messages/<id> ---------------------------


async def test_delete_messages_removes_row(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        posted = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "delete me",
            },
        ).json()
        msg_id = posted["message_id"]

        r = admin.request(
            "DELETE",
            f"/api/messages/{msg_id}",
            json={},
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True

        listing = admin.post(
            "/api/messages/query", json={}
        ).json()
        ids = [m["message_id"] for m in listing["messages"]]
        assert msg_id not in ids, (
            "deleted message_id should no longer appear in "
            "/api/messages/query"
        )


async def test_delete_messages_404_on_unknown_id(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "DELETE",
            "/api/messages/msg_doesnotexist",
            json={},
        )
        assert r.status_code == 404, r.text


async def test_delete_messages_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        posted = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "hi",
            },
        ).json()
        r = admin.client.request(
            "DELETE",
            f"/api/messages/{posted['message_id']}",
            # Fake bearer: exercises the operator-tier gate, not no-auth 401.
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={},
        )
        assert r.status_code == 401, r.text


# ---------- Broadcast via POST /api/messages -------------------


async def test_post_messages_broadcast_fans_out_to_workers(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "*",
                "message_content": "hello everyone",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True, body
        assert body.get("broadcast") is True, body
        assert body.get("sent_count", 0) >= 2, body

        listing = admin.post(
            "/api/messages/query", json={},
        ).json()
        recipients = {
            m["recipient_id"]
            for m in listing["messages"]
            if m["message_content"] == "hello everyone"
        }
        assert "alice" in recipients, recipients
        assert "bob" in recipients, recipients
        # Admin must not receive its own broadcast.
        assert "admin" not in recipients, recipients


async def test_post_messages_broadcast_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.client.post(
            "/api/messages",
            # Fake bearer: exercises the operator-tier gate, not no-auth 401.
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={
                "recipient_id": "*",
                "message_content": "nope",
            },
        )
        assert r.status_code == 401, r.text


# ---------- POST /api/messages/participants --------------------


async def test_participants_lists_live_agents_only(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _seed_terminated_worker(admin, "bob")

        r = admin.post(
            "/api/messages/participants",
            json={},
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert "live" in body, body
        assert "tombstones" in body, body

        live_ids = [a["agent_id"] for a in body["live"]]
        assert "alice" in live_ids, live_ids
        assert "bob" not in live_ids, (
            f"terminated agent leaked into participants.live: {live_ids}"
        )


async def test_participants_includes_admin_in_live(tmp_path) -> None:
    # admin is a synthetic always-present sender; the agents table does
    # not contain an "admin" row, so the endpoint must inject it so
    # admins can filter for messages they themselves sent.
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/messages/participants",
            json={},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        live_ids = [a["agent_id"] for a in body["live"]]
        assert "admin" in live_ids or "Admin" in live_ids, (
            f"expected 'admin' (or 'Admin') to be injected as a live "
            f"participant; got {live_ids}"
        )


async def test_participants_lists_tombstones(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # PR-G1 added FK constraints on agent_messages.{sender_id,
        # recipient_id} -> agents.agent_id. Tombstone strings live in
        # those columns once an agent is purged; tests that
        # pre-populate tombstones must seed the agents row too. In
        # production the cascade in routes.py inserts these rows
        # automatically before the UPDATE.
        seed_agent_rows("[deleted-old-worker-1]", "[deleted-old-worker-2]")
        # PR C tombstone marker on a sender_id and a recipient_id.
        _seed_message_with_sender(
            "[deleted-old-worker-1]", recipient_id="alice", content="legacy",
        )
        _seed_message_with_sender(
            "admin",
            recipient_id="[deleted-old-worker-2]",
            content="legacy2",
        )

        r = admin.post(
            "/api/messages/participants",
            json={},
        )
        assert r.status_code == 200, r.text
        tombstones = r.json().get("tombstones", [])
        assert "[deleted-old-worker-1]" in tombstones, tombstones
        assert "[deleted-old-worker-2]" in tombstones, tombstones


async def test_participants_tombstones_distinct_and_sorted(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # See test_participants_lists_tombstones for FK seeding rationale.
        seed_agent_rows("[deleted-zzz]", "[deleted-aaa]")
        # Duplicate the same tombstone across multiple messages; the
        # endpoint must DISTINCT them.
        for _ in range(3):
            _seed_message_with_sender("[deleted-zzz]", recipient_id="alice")
        _seed_message_with_sender("[deleted-aaa]", recipient_id="alice")

        r = admin.post(
            "/api/messages/participants",
            json={},
        )
        tombstones = r.json().get("tombstones", [])
        # Distinct: each appears exactly once.
        assert tombstones.count("[deleted-zzz]") == 1, tombstones
        assert tombstones.count("[deleted-aaa]") == 1, tombstones
        # Sorted lexicographically (aaa before zzz).
        assert tombstones.index("[deleted-aaa]") < tombstones.index(
            "[deleted-zzz]"
        ), (
            f"tombstones should be sorted lexicographically: {tombstones}"
        )


async def test_participants_empty_tombstones_when_none(tmp_path) -> None:
    # PR C has not landed yet; today the message table contains no
    # tombstone rows. The endpoint must return an empty list, not error.
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        r = admin.post(
            "/api/messages/participants",
            json={},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tombstones"] == [], body


async def test_participants_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/messages/participants",
            # Fake bearer: exercises the operator-tier gate, not no-auth 401.
            headers={"Authorization": f"Bearer {'x' * 32}"},
            json={},
        )
        assert r.status_code == 401, r.text
