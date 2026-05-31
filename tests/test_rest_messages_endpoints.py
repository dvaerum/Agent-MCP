"""REST endpoints for agent_messages (Phase 6 PR #20).

The dashboard's new Messages tab needs:
- GET /api/messages with filters (from/to/between/type/priority/
  read/since/until/q/limit/offset)
- POST /api/messages — admin compose
- PATCH /api/messages/<id> — flip read/delivered for admin housekeeping

Token in JSON body, matching the Q6a.1 convention used by the
existing memory + task endpoints.
"""

from __future__ import annotations

import datetime as _dt
import secrets


def _admin(client) -> str:
    return client.get("/api/tokens").json()["admin_token"]


def _seed_worker(name: str, status: str = "active") -> str:
    """Insert a worker row + register in globals. Returns the token.

    ``status`` defaults to ``active``; pass ``terminated`` to simulate a
    worker that has been ended (used by the participants endpoint tests
    to assert ghost agents are filtered out of the dashboard dropdowns).
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, name, "[]", now, status, "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()

    g.active_agents[token] = {
        "agent_id": name,
        "status": status,
        "created_at": now,
        "capabilities": [],
    }
    return token


def _seed_message_with_sender(sender_id: str, recipient_id: str = "alice",
                              content: str = "x") -> str:
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
        "message_content, message_type, priority, timestamp, delivered, read) "
        "VALUES (?, ?, ?, ?, 'text', 'normal', ?, 0, 0)",
        (msg_id, sender_id, recipient_id, content, now),
    )
    conn.commit()
    conn.close()
    return msg_id


# ---------- POST /api/messages ----------------------------------


def test_post_messages_creates_message_with_admin_token(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")

    r = client.post(
        "/api/messages",
        json={
            "token": admin,
            "recipient_id": "alice",
            "message_content": "hello alice",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("success") is True, body
    assert "message_id" in body, body


def test_post_messages_rejects_bad_token(client) -> None:
    _seed_worker("alice")
    r = client.post(
        "/api/messages",
        json={
            "token": "x" * 32,
            "recipient_id": "alice",
            "message_content": "hi",
        },
    )
    assert r.status_code == 403, r.text


def test_post_messages_rejects_missing_recipient(client) -> None:
    admin = _admin(client)
    r = client.post(
        "/api/messages",
        json={"token": admin, "message_content": "lonely"},
    )
    assert r.status_code == 400, r.text


def test_post_messages_rejects_missing_content(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    r = client.post(
        "/api/messages",
        json={"token": admin, "recipient_id": "alice"},
    )
    assert r.status_code == 400, r.text


# ---------- POST /api/messages/query (filtered listing) -------------


def test_list_uses_post_query_not_get_with_body(client) -> None:
    """Regression: GET /api/messages returns 405. Filtered listing
    must be POST /api/messages/query because the Fetch spec strips
    bodies from GET requests in the browser.
    """
    admin = _admin(client)
    r = client.request("GET", "/api/messages", json={"token": admin})
    assert r.status_code in (404, 405), (
        f"GET /api/messages should no longer be a route; got {r.status_code}"
    )


# ---------- POST /api/messages/query ---------------------------


def test_get_messages_lists_seeded_messages(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")

    # Seed via POST.
    posted = client.post(
        "/api/messages",
        json={
            "token": admin,
            "recipient_id": "alice",
            "message_content": "findable",
        },
    ).json()
    msg_id = posted["message_id"]

    # GET requires admin token in query or body (admin in body for
    # consistency with the convention).
    r = client.post(
        "/api/messages/query",
        json={"token": admin},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "messages" in body
    ids = [m["message_id"] for m in body["messages"]]
    assert msg_id in ids


def test_get_messages_filter_from(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    _seed_worker("bob")

    client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "to alice from admin"
    })

    r = client.post("/api/messages/query", json={
        "token": admin, "from": "admin"
    })
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert all(m["sender_id"] == "admin" for m in msgs), (
        f"filter from=admin returned non-admin senders: {[m['sender_id'] for m in msgs]}"
    )


def test_get_messages_filter_to(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    _seed_worker("bob")

    client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "to alice"
    })
    client.post("/api/messages", json={
        "token": admin, "recipient_id": "bob", "message_content": "to bob"
    })

    r = client.post("/api/messages/query", json={
        "token": admin, "to": "alice"
    })
    msgs = r.json()["messages"]
    assert all(m["recipient_id"] == "alice" for m in msgs), (
        f"filter to=alice returned others: {[m['recipient_id'] for m in msgs]}"
    )
    assert len(msgs) >= 1


def test_get_messages_filter_read(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")

    client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "unread one"
    })

    # All seeded messages start with read=False.
    r = client.post("/api/messages/query", json={
        "token": admin, "read": False
    })
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert all(m["read"] == 0 or m["read"] is False for m in msgs)
    assert len(msgs) >= 1


def test_get_messages_filter_content_substring(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")

    client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "pineapple pizza"
    })
    client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "boring text"
    })

    r = client.post("/api/messages/query", json={
        "token": admin, "q": "pineapple"
    })
    msgs = r.json()["messages"]
    assert any("pineapple" in m["message_content"] for m in msgs)
    assert all("boring" not in m["message_content"] for m in msgs)


def test_get_messages_pagination(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    # Seed >2 messages.
    for i in range(3):
        client.post("/api/messages", json={
            "token": admin, "recipient_id": "alice", "message_content": f"msg {i}"
        })

    r = client.post("/api/messages/query", json={
        "token": admin, "limit": 2, "offset": 0
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["messages"]) <= 2


def test_get_messages_rejects_bad_token(client) -> None:
    r = client.post("/api/messages/query", json={"token": "x" * 32})
    assert r.status_code == 403, r.text


# ---------- PATCH /api/messages/<id> ----------------------------


def test_patch_messages_marks_read(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    posted = client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "to be read"
    }).json()
    msg_id = posted["message_id"]

    r = client.request("PATCH", f"/api/messages/{msg_id}", json={
        "token": admin, "read": True
    })
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True

    # Verify via GET.
    listing = client.post("/api/messages/query", json={
        "token": admin
    }).json()
    msg = next(m for m in listing["messages"] if m["message_id"] == msg_id)
    assert msg["read"] in (1, True)


def test_patch_messages_404_on_unknown_id(client) -> None:
    admin = _admin(client)
    r = client.request("PATCH", "/api/messages/msg_doesnotexist", json={
        "token": admin, "read": True
    })
    assert r.status_code == 404, r.text


def test_patch_messages_rejects_bad_token(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    posted = client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "hi"
    }).json()
    r = client.request("PATCH", f"/api/messages/{posted['message_id']}", json={
        "token": "x" * 32, "read": True
    })
    assert r.status_code == 403, r.text


# ---------- DELETE /api/messages/<id> ---------------------------
# The Messages tab needs row-level delete + bulk delete from a
# selection toolbar. We expose a thin DELETE endpoint that wraps the
# existing admin-only message housekeeping path.


def test_delete_messages_removes_row(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    posted = client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "delete me"
    }).json()
    msg_id = posted["message_id"]

    r = client.request("DELETE", f"/api/messages/{msg_id}", json={
        "token": admin,
    })
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True

    listing = client.post("/api/messages/query", json={"token": admin}).json()
    ids = [m["message_id"] for m in listing["messages"]]
    assert msg_id not in ids, (
        "deleted message_id should no longer appear in /api/messages/query"
    )


def test_delete_messages_404_on_unknown_id(client) -> None:
    admin = _admin(client)
    r = client.request("DELETE", "/api/messages/msg_doesnotexist", json={
        "token": admin,
    })
    assert r.status_code == 404, r.text


def test_delete_messages_rejects_bad_token(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    posted = client.post("/api/messages", json={
        "token": admin, "recipient_id": "alice", "message_content": "hi"
    }).json()
    r = client.request("DELETE", f"/api/messages/{posted['message_id']}", json={
        "token": "x" * 32,
    })
    assert r.status_code == 403, r.text


# ---------- Broadcast via POST /api/messages -------------------
# The dashboard Compose form needs a "(broadcast to all workers)" option.
# We extend POST /api/messages to accept recipient_id="*" and fan out to
# every active worker (admin excluded), mirroring the broadcast_message
# admin MCP tool. Returns sent_count instead of a single message_id.


def test_post_messages_broadcast_fans_out_to_workers(client) -> None:
    admin = _admin(client)
    _seed_worker("alice")
    _seed_worker("bob")

    r = client.post("/api/messages", json={
        "token": admin,
        "recipient_id": "*",
        "message_content": "hello everyone",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("success") is True, body
    assert body.get("broadcast") is True, body
    assert body.get("sent_count", 0) >= 2, body

    listing = client.post("/api/messages/query", json={"token": admin}).json()
    recipients = {m["recipient_id"] for m in listing["messages"]
                  if m["message_content"] == "hello everyone"}
    assert "alice" in recipients, recipients
    assert "bob" in recipients, recipients
    # Admin must not receive its own broadcast.
    assert "admin" not in recipients, recipients


def test_post_messages_broadcast_rejects_bad_token(client) -> None:
    _seed_worker("alice")
    r = client.post("/api/messages", json={
        "token": "x" * 32,
        "recipient_id": "*",
        "message_content": "nope",
    })
    assert r.status_code == 403, r.text


# ---------- POST /api/messages/participants --------------------
# The Messages tab's From/To filter dropdowns originally sourced from
# /api/agents, which returns EVERY agent row including status='terminated'.
# Dennis flagged ghost agents in the dropdown that no longer existed in the
# Agents page. The fix introduces a dedicated /participants endpoint that
# returns (a) live agents (status != 'terminated') and (b) DISTINCT
# tombstone strings (sender_id / recipient_id beginning with
# ``[deleted-``) so admins can still grep history for purged agents
# (PR C cascade).


def test_participants_lists_live_agents_only(client) -> None:
    admin = _admin(client)
    _seed_worker("alice", status="active")
    _seed_worker("bob", status="terminated")

    r = client.post("/api/messages/participants", json={"token": admin})
    assert r.status_code == 200, r.text
    body = r.json()

    assert "live" in body, body
    assert "tombstones" in body, body

    live_ids = [a["agent_id"] for a in body["live"]]
    assert "alice" in live_ids, live_ids
    assert "bob" not in live_ids, (
        f"terminated agent leaked into participants.live: {live_ids}"
    )


def test_participants_includes_admin_in_live(client) -> None:
    # admin is a synthetic always-present sender; the agents table does
    # not contain an "admin" row, so the endpoint must inject it so
    # admins can filter for messages they themselves sent.
    admin = _admin(client)
    r = client.post("/api/messages/participants", json={"token": admin})
    assert r.status_code == 200, r.text
    body = r.json()
    live_ids = [a["agent_id"] for a in body["live"]]
    assert "admin" in live_ids or "Admin" in live_ids, (
        f"expected 'admin' (or 'Admin') to be injected as a live "
        f"participant; got {live_ids}"
    )


def test_participants_lists_tombstones(client) -> None:
    admin = _admin(client)
    _seed_worker("alice", status="active")
    # PR C tombstone marker on a sender_id and a recipient_id.
    _seed_message_with_sender(
        "[deleted-old-worker-1]", recipient_id="alice", content="legacy"
    )
    _seed_message_with_sender(
        "admin", recipient_id="[deleted-old-worker-2]", content="legacy2"
    )

    r = client.post("/api/messages/participants", json={"token": admin})
    assert r.status_code == 200, r.text
    tombstones = r.json().get("tombstones", [])
    assert "[deleted-old-worker-1]" in tombstones, tombstones
    assert "[deleted-old-worker-2]" in tombstones, tombstones


def test_participants_tombstones_distinct_and_sorted(client) -> None:
    admin = _admin(client)
    _seed_worker("alice", status="active")
    # Duplicate the same tombstone across multiple messages; the endpoint
    # must DISTINCT them.
    for _ in range(3):
        _seed_message_with_sender("[deleted-zzz]", recipient_id="alice")
    _seed_message_with_sender("[deleted-aaa]", recipient_id="alice")

    r = client.post("/api/messages/participants", json={"token": admin})
    tombstones = r.json().get("tombstones", [])
    # Distinct: each appears exactly once.
    assert tombstones.count("[deleted-zzz]") == 1, tombstones
    assert tombstones.count("[deleted-aaa]") == 1, tombstones
    # Sorted lexicographically (aaa before zzz).
    assert tombstones.index("[deleted-aaa]") < tombstones.index("[deleted-zzz]"), (
        f"tombstones should be sorted lexicographically: {tombstones}"
    )


def test_participants_empty_tombstones_when_none(client) -> None:
    # PR C has not landed yet; today the message table contains no
    # tombstone rows. The endpoint must return an empty list, not error.
    admin = _admin(client)
    _seed_worker("alice", status="active")

    r = client.post("/api/messages/participants", json={"token": admin})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tombstones"] == [], body


def test_participants_rejects_bad_token(client) -> None:
    r = client.post(
        "/api/messages/participants", json={"token": "x" * 32}
    )
    assert r.status_code == 403, r.text
