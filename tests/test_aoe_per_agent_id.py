"""AoE session id is per-agent, set via the dashboard's agent edit dialog.

The first design auto-resolved AoE id by matching ``title ==
recipient_id`` against /api/sessions. That's brittle: AoE titles are
free-form, the user might rename them, and there's no first-class
record of which AoE session belongs to which agent-mcp worker.

This iteration:

* Adds an ``aoe_session_id`` column to the ``agents`` table
  (migration 0003).
* Lets the admin set/clear it through the existing
  ``POST /api/agents/<id>/edit`` endpoint.
* The notifier prefers the stored per-agent id; only falls back to
  title-match resolution when the column is empty (backwards-compat).
* Token can be sourced from a file path
  (``config_aoe_bearer_token_file``) — useful because AoE rotates
  ``~/.config/agent-of-empires/serve.token`` periodically.
* New ``GET /api/aoe/health`` endpoint pings AoE with the current
  credentials and reports back so the dashboard can warn when the
  token is stale.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import secrets
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Shared helpers (copied from test_aoe_notification.py to keep the two
# test files independently runnable)
# ---------------------------------------------------------------------------

def _admin_token(client) -> str:
    return client.get("/api/tokens").json()["admin_token"]


def _seed_worker(name: str = "alice", *, aoe_session_id: str | None = None):
    """Register a worker. Returns (token, agent_id). Tries to write
    the new aoe_session_id column too — falls back gracefully if the
    migration hasn't run yet (so the red phase can still load).
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    worker_token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO agents (token, agent_id, capabilities, created_at, "
            "status, working_directory, color, updated_at, aoe_session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (worker_token, name, "[]", now, "active", "/tmp", "#888", now,
             aoe_session_id),
        )
    except Exception:
        cursor.execute(
            "INSERT INTO agents (token, agent_id, capabilities, created_at, "
            "status, working_directory, color, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (worker_token, name, "[]", now, "active", "/tmp", "#888", now),
        )
    conn.commit()
    conn.close()

    g.active_agents[worker_token] = {
        "agent_id": name,
        "status": "active",
        "created_at": now,
        "capabilities": [],
    }
    return worker_token, name


def _set_ctx(client, key: str, value: Any, admin_token: str) -> None:
    r = client.post(
        "/api/memories",
        json={"token": admin_token, "context_key": key, "context_value": value},
    )
    if r.status_code == 409:
        r = client.request(
            "PUT",
            f"/api/memories/{key}",
            json={"token": admin_token, "context_value": value},
        )
    assert r.status_code == 200, r.text


async def _send_and_wait(sender_token: str, recipient_id: str, body: str = "hi"):
    from agent_mcp.tools.agent_communication_tools import send_agent_message_tool_impl

    result = await send_agent_message_tool_impl({
        "token": sender_token,
        "recipient_id": recipient_id,
        "message": body,
        "deliver_method": "store",
    })
    for _ in range(20):
        await asyncio.sleep(0)
    return result


class _AoeServer:
    """Same mock as test_aoe_notification, slightly extended."""

    def __init__(self) -> None:
        self.sessions: list[dict[str, Any]] = []
        self.send_status: int = 200
        self.send_status_per_call: list[int] = []
        self.sends: list[tuple[str, dict, str | None]] = []
        self.sessions_list_calls: int = 0
        # Bearer tokens the server will accept. ``None`` element means
        # "no Authorization header required". Any other token →
        # /api/sessions returns 401.
        self.accepted_tokens: set[str | None] = {None}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        auth = request.headers.get("authorization")
        bearer: str | None = None
        if auth and auth.lower().startswith("bearer "):
            bearer = auth.split(" ", 1)[1].strip()

        if request.method == "GET" and path == "/api/sessions":
            self.sessions_list_calls += 1
            if bearer not in self.accepted_tokens:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"sessions": list(self.sessions)})

        if request.method == "POST" and path.startswith("/api/sessions/") and path.endswith("/send"):
            aoe_id = path[len("/api/sessions/"):-len("/send")]
            body = json.loads(request.read() or b"{}")
            self.sends.append((aoe_id, body, auth))
            if bearer not in self.accepted_tokens:
                return httpx.Response(401, json={"error": "unauthorized"})
            status = (
                self.send_status_per_call.pop(0)
                if self.send_status_per_call
                else self.send_status
            )
            if status == 200:
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(status, json={"error": "boom"})

        return httpx.Response(404, json={"error": "unhandled"})


@pytest.fixture
def aoe_mock(monkeypatch):
    server = _AoeServer()
    transport = httpx.MockTransport(server.handler)
    from agent_mcp.features import aoe_notify

    aoe_notify.clear_session_cache()
    monkeypatch.setattr(aoe_notify, "_TRANSPORT_FOR_TESTS", transport, raising=False)
    yield server
    aoe_notify.clear_session_cache()


# ---------------------------------------------------------------------------
# 1. Schema: aoe_session_id column on agents
# ---------------------------------------------------------------------------

def test_agents_table_has_aoe_session_id_column(client) -> None:
    """Migration 0003 must add aoe_session_id (nullable TEXT) to agents."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(agents)")
    cols = {row["name"] for row in cur.fetchall()}
    conn.close()
    assert "aoe_session_id" in cols, (
        "agents table is missing the aoe_session_id column "
        "(migration 0003 not applied or revision mis-chained)"
    )


# ---------------------------------------------------------------------------
# 2. Per-agent id wins over title-match resolution
# ---------------------------------------------------------------------------

def test_notifier_uses_stored_aoe_session_id(client, aoe_mock) -> None:
    """When the agent has aoe_session_id set, the notifier POSTs to
    THAT id and skips the /api/sessions lookup entirely.
    """
    admin = _admin_token(client)
    _set_ctx(client, "config_aoe_notify_enabled", "true", admin)
    _set_ctx(client, "config_aoe_base_url", "http://aoe.test", admin)
    # Title says "someone_else" but the stored id wins.
    aoe_mock.sessions = [
        {"id": "ffffffffffffffff", "title": "someone_else", "status": "Running"},
    ]
    _seed_worker("alice", aoe_session_id="abc123def456cafe")

    asyncio.run(_send_and_wait(admin, "alice"))

    assert len(aoe_mock.sends) == 1
    aoe_id, _, _ = aoe_mock.sends[0]
    assert aoe_id == "abc123def456cafe"
    # Fast-path: no /api/sessions probe needed.
    assert aoe_mock.sessions_list_calls == 0


def test_notifier_falls_back_to_title_match(client, aoe_mock) -> None:
    """When aoe_session_id is empty, the old title-match resolution
    still kicks in (backwards-compat)."""
    admin = _admin_token(client)
    _set_ctx(client, "config_aoe_notify_enabled", "true", admin)
    _set_ctx(client, "config_aoe_base_url", "http://aoe.test", admin)
    aoe_mock.sessions = [
        {"id": "deadbeefcafe0000", "title": "alice", "status": "Running"},
    ]
    _seed_worker("alice", aoe_session_id=None)

    asyncio.run(_send_and_wait(admin, "alice"))

    assert len(aoe_mock.sends) == 1
    aoe_id, _, _ = aoe_mock.sends[0]
    assert aoe_id == "deadbeefcafe0000"
    assert aoe_mock.sessions_list_calls == 1


# ---------------------------------------------------------------------------
# 3. Dashboard edit endpoint accepts aoe_session_id
# ---------------------------------------------------------------------------

def test_edit_endpoint_sets_aoe_session_id(client) -> None:
    """POST /api/agents/<id>/edit must accept aoe_session_id."""
    _seed_worker("alice")
    admin = _admin_token(client)
    r = client.post(
        "/api/agents/alice/edit",
        json={"token": admin, "aoe_session_id": "1234567890abcdef"},
    )
    assert r.status_code == 200, r.text

    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT aoe_session_id FROM agents WHERE agent_id = ?", ("alice",))
    row = cur.fetchone()
    conn.close()
    assert row is not None
    assert row["aoe_session_id"] == "1234567890abcdef"


def test_edit_endpoint_clears_aoe_session_id_with_empty_string(client) -> None:
    """Sending an empty string must NULL out the column (clears the binding)."""
    _seed_worker("alice", aoe_session_id="cafebabecafebabe")
    admin = _admin_token(client)

    r = client.post(
        "/api/agents/alice/edit",
        json={"token": admin, "aoe_session_id": ""},
    )
    assert r.status_code == 200, r.text

    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT aoe_session_id FROM agents WHERE agent_id = ?", ("alice",))
    row = cur.fetchone()
    conn.close()
    assert row["aoe_session_id"] in (None, "")


def test_edit_endpoint_validates_aoe_session_id_format(client) -> None:
    """AoE ids are 16 lowercase hex chars. Anything else → 400."""
    _seed_worker("alice")
    admin = _admin_token(client)

    for bad in (
        "not-hex",
        "tooshort",
        "1234567890abcdef1",  # too long
        "GHIJKLMNOPQRSTUV",   # not hex
        "abcdefghij123456",   # invalid hex chars
    ):
        r = client.post(
            "/api/agents/alice/edit",
            json={"token": admin, "aoe_session_id": bad},
        )
        assert r.status_code == 400, f"{bad!r} should be rejected: {r.text}"


# ---------------------------------------------------------------------------
# 4. Bearer token can be sourced from a file
# ---------------------------------------------------------------------------

def test_token_loaded_from_file(client, aoe_mock, tmp_path) -> None:
    """config_aoe_bearer_token_file → read token from disk on each call.

    AoE rotates ~/.config/agent-of-empires/serve.token on a schedule;
    keeping the file path in config (and re-reading on use) means we
    survive a rotation without an admin restart.
    """
    admin = _admin_token(client)
    token_path = tmp_path / "serve.token"
    token_path.write_text("file-sourced-token-123\n")
    _set_ctx(client, "config_aoe_notify_enabled", "true", admin)
    _set_ctx(client, "config_aoe_base_url", "http://aoe.test", admin)
    _set_ctx(client, "config_aoe_bearer_token_file", str(token_path), admin)
    aoe_mock.accepted_tokens = {"file-sourced-token-123"}
    _seed_worker("alice", aoe_session_id="a" * 16)

    asyncio.run(_send_and_wait(admin, "alice"))

    assert len(aoe_mock.sends) == 1
    _, _, auth = aoe_mock.sends[0]
    assert auth == "Bearer file-sourced-token-123"


def test_token_file_rotation_picked_up_without_restart(client, aoe_mock, tmp_path) -> None:
    """Writing a new token to the same file → subsequent sends use it."""
    admin = _admin_token(client)
    token_path = tmp_path / "serve.token"
    token_path.write_text("rev1\n")
    _set_ctx(client, "config_aoe_notify_enabled", "true", admin)
    _set_ctx(client, "config_aoe_base_url", "http://aoe.test", admin)
    _set_ctx(client, "config_aoe_bearer_token_file", str(token_path), admin)
    aoe_mock.accepted_tokens = {"rev1"}
    _seed_worker("alice", aoe_session_id="b" * 16)

    asyncio.run(_send_and_wait(admin, "alice"))
    token_path.write_text("rev2\n")
    aoe_mock.accepted_tokens = {"rev2"}
    asyncio.run(_send_and_wait(admin, "alice"))

    assert len(aoe_mock.sends) == 2
    assert aoe_mock.sends[0][2] == "Bearer rev1"
    assert aoe_mock.sends[1][2] == "Bearer rev2"


def test_inline_token_takes_precedence_over_file(client, aoe_mock, tmp_path) -> None:
    """If both config_aoe_bearer_token AND ..._file are set, the
    inline value wins (explicit-over-implicit). Keeps the existing
    one-secret-key workflow unchanged.
    """
    admin = _admin_token(client)
    token_path = tmp_path / "serve.token"
    token_path.write_text("from-file\n")
    _set_ctx(client, "config_aoe_notify_enabled", "true", admin)
    _set_ctx(client, "config_aoe_base_url", "http://aoe.test", admin)
    _set_ctx(client, "config_aoe_bearer_token", "from-inline", admin)
    _set_ctx(client, "config_aoe_bearer_token_file", str(token_path), admin)
    aoe_mock.accepted_tokens = {"from-inline"}
    _seed_worker("alice", aoe_session_id="c" * 16)

    asyncio.run(_send_and_wait(admin, "alice"))
    assert aoe_mock.sends[-1][2] == "Bearer from-inline"


def test_missing_token_file_does_not_crash(client, aoe_mock, tmp_path) -> None:
    """Path points at a non-existent file → log + skip the send, no
    exception bubbles."""
    admin = _admin_token(client)
    _set_ctx(client, "config_aoe_notify_enabled", "true", admin)
    _set_ctx(client, "config_aoe_base_url", "http://aoe.test", admin)
    _set_ctx(client, "config_aoe_bearer_token_file", str(tmp_path / "nope"), admin)
    _seed_worker("alice", aoe_session_id="d" * 16)

    res = asyncio.run(_send_and_wait(admin, "alice"))
    assert "denied" not in res[0].text.lower()
    # We didn't have a usable token → no POST attempted.
    assert aoe_mock.sends == []


# ---------------------------------------------------------------------------
# 5. Health check endpoint
# ---------------------------------------------------------------------------

def test_aoe_health_ok(client, aoe_mock) -> None:
    """GET /api/aoe/health hits AoE, reports ok + session count."""
    admin = _admin_token(client)
    _set_ctx(client, "config_aoe_notify_enabled", "true", admin)
    _set_ctx(client, "config_aoe_base_url", "http://aoe.test", admin)
    _set_ctx(client, "config_aoe_bearer_token", "good-token", admin)
    aoe_mock.accepted_tokens = {"good-token"}
    aoe_mock.sessions = [
        {"id": "0" * 16, "title": "alice", "status": "Running"},
        {"id": "1" * 16, "title": "bob", "status": "Idle"},
    ]

    r = client.get(f"/api/aoe/health?token={admin}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["session_count"] == 2


def test_aoe_health_bad_token(client, aoe_mock) -> None:
    """If AoE rejects the configured token, health reports unauthorized."""
    admin = _admin_token(client)
    _set_ctx(client, "config_aoe_notify_enabled", "true", admin)
    _set_ctx(client, "config_aoe_base_url", "http://aoe.test", admin)
    _set_ctx(client, "config_aoe_bearer_token", "stale-token", admin)
    aoe_mock.accepted_tokens = {"the-current-token"}  # NOT "stale-token"

    r = client.get(f"/api/aoe/health?token={admin}")
    assert r.status_code == 200, r.text  # endpoint itself succeeds
    body = r.json()
    assert body["status"] == "unauthorized", body
    assert "401" in body.get("message", "")


def test_aoe_health_requires_admin(client) -> None:
    """No token / worker token → 401/403."""
    r = client.get("/api/aoe/health")
    assert r.status_code in (401, 403)


def test_aoe_health_disabled_status(client, aoe_mock) -> None:
    """When the master toggle is off, health says 'disabled' (don't
    probe AoE at all)."""
    admin = _admin_token(client)
    # No config_aoe_notify_enabled set (defaults to off).
    r = client.get(f"/api/aoe/health?token={admin}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "disabled", body
    # No HTTP traffic happened.
    assert aoe_mock.sessions_list_calls == 0
