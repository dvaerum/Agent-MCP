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

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tests.harness import mcp_session, seed_config_setting_as_sysadmin

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _make_worker(
    admin, name: str = "alice", *, aoe_session_id: str | None = None
):
    """Register a worker via the harness, then optionally backfill
    aoe_session_id on the row (the harness's create_worker doesn't take
    that column)."""
    worker = await admin.create_worker(name)
    if aoe_session_id is not None:
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE agents SET aoe_session_id = ? WHERE agent_id = ?",
            (aoe_session_id, name),
        )
        conn.commit()
        conn.close()
    return worker


def _set_ctx(admin, key: str, value: Any) -> None:
    # config_aoe_* is sysadmin-only to write (pentest R8-F1) — the
    # operator-tier REST seam below now 403s on it, so seed it as a
    # sysadmin would. Other config/context keys stay on the REST path.
    if key.lower().startswith("config_aoe_"):
        seed_config_setting_as_sysadmin(key, value)
        return
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
        },
    )
    if r.status_code == 409:
        r = admin.request(
            "PUT",
            f"/api/memories/{key}",
            json={"context_value": value},
        )
    assert r.status_code == 200, r.text


async def _send_and_wait(admin, recipient_id: str, body: str = "hi"):
    result = await admin.call(
        "send_agent_message",
        {
            "recipient_id": recipient_id,
            "message": body,
            "deliver_method": "store",
        },
    )
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

        if (
            request.method == "POST"
            and path.startswith("/api/sessions/")
            and path.endswith("/send")
        ):
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
    monkeypatch.setattr(
        aoe_notify, "_TRANSPORT_FOR_TESTS", transport, raising=False
    )
    yield server
    aoe_notify.clear_session_cache()


# ---------------------------------------------------------------------------
# 1. Schema: aoe_session_id column on agents
# ---------------------------------------------------------------------------


async def test_agents_table_has_aoe_session_id_column(tmp_path) -> None:
    """Migration 0003 must add aoe_session_id (nullable TEXT) to agents."""
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
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


async def test_notifier_uses_stored_aoe_session_id(tmp_path, aoe_mock) -> None:
    """When the agent has aoe_session_id set, the notifier POSTs to
    THAT id and skips the /api/sessions lookup entirely.
    """
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        # Title says "someone_else" but the stored id wins.
        aoe_mock.sessions = [
            {"id": "ffffffffffffffff", "title": "someone_else", "status": "Running"},
        ]
        await _make_worker(admin, "alice", aoe_session_id="abc123def456cafe")

        await _send_and_wait(admin, "alice")

        assert len(aoe_mock.sends) == 1
        aoe_id, _, _ = aoe_mock.sends[0]
        assert aoe_id == "abc123def456cafe"
        # Fast-path: no /api/sessions probe needed.
        assert aoe_mock.sessions_list_calls == 0


async def test_notifier_falls_back_to_title_match(tmp_path, aoe_mock) -> None:
    """When aoe_session_id is empty, the old title-match resolution
    still kicks in (backwards-compat)."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        aoe_mock.sessions = [
            {"id": "deadbeefcafe0000", "title": "alice", "status": "Running"},
        ]
        await _make_worker(admin, "alice", aoe_session_id=None)

        await _send_and_wait(admin, "alice")

        assert len(aoe_mock.sends) == 1
        aoe_id, _, _ = aoe_mock.sends[0]
        assert aoe_id == "deadbeefcafe0000"
        assert aoe_mock.sessions_list_calls == 1


# ---------------------------------------------------------------------------
# 3. Dashboard edit endpoint accepts aoe_session_id
# ---------------------------------------------------------------------------


async def test_edit_endpoint_sets_aoe_session_id(tmp_path) -> None:
    """POST /api/agents/<id>/edit must accept aoe_session_id."""
    async with mcp_session(tmp_path) as admin:
        await _make_worker(admin, "alice")
        r = admin.post(
            "/api/agents/alice/edit",
            json={
                "aoe_session_id": "1234567890abcdef",
            },
        )
        assert r.status_code == 200, r.text

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT aoe_session_id FROM agents WHERE agent_id = ?",
            ("alice",),
        )
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row["aoe_session_id"] == "1234567890abcdef"


async def test_edit_endpoint_clears_aoe_session_id_with_empty_string(
    tmp_path,
) -> None:
    """Sending an empty string must NULL out the column (clears the
    binding)."""
    async with mcp_session(tmp_path) as admin:
        await _make_worker(admin, "alice", aoe_session_id="cafebabecafebabe")

        r = admin.post(
            "/api/agents/alice/edit",
            json={"aoe_session_id": ""},
        )
        assert r.status_code == 200, r.text

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT aoe_session_id FROM agents WHERE agent_id = ?",
            ("alice",),
        )
        row = cur.fetchone()
        conn.close()
        assert row["aoe_session_id"] in (None, "")


async def test_edit_endpoint_validates_aoe_session_id_format(tmp_path) -> None:
    """AoE ids are 16 lowercase hex chars. Anything else → 400."""
    async with mcp_session(tmp_path) as admin:
        await _make_worker(admin, "alice")

        for bad in (
            "not-hex",
            "tooshort",
            "1234567890abcdef1",  # too long
            "GHIJKLMNOPQRSTUV",   # not hex
            "abcdefghij123456",   # invalid hex chars
        ):
            r = admin.post(
                "/api/agents/alice/edit",
                json={"aoe_session_id": bad},
            )
            assert r.status_code == 400, (
                f"{bad!r} should be rejected: {r.text}"
            )


# ---------------------------------------------------------------------------
# 4. Bearer token can be sourced from a file
# ---------------------------------------------------------------------------


async def test_token_loaded_from_file(tmp_path, aoe_mock) -> None:
    """config_aoe_bearer_token_file → read token from disk on each call.

    AoE rotates ~/.config/agent-of-empires/serve.token on a schedule;
    keeping the file path in config (and re-reading on use) means we
    survive a rotation without an admin restart.
    """
    async with mcp_session(tmp_path) as admin:
        token_path = tmp_path / "serve.token"
        token_path.write_text("file-sourced-token-123\n")
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(admin, "config_aoe_bearer_token_file", str(token_path))
        aoe_mock.accepted_tokens = {"file-sourced-token-123"}
        await _make_worker(admin, "alice", aoe_session_id="a" * 16)

        await _send_and_wait(admin, "alice")

        assert len(aoe_mock.sends) == 1
        _, _, auth = aoe_mock.sends[0]
        assert auth == "Bearer file-sourced-token-123"


async def test_token_file_rotation_picked_up_without_restart(
    tmp_path, aoe_mock,
) -> None:
    """Writing a new token to the same file → subsequent sends use it."""
    async with mcp_session(tmp_path) as admin:
        token_path = tmp_path / "serve.token"
        token_path.write_text("rev1\n")
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(admin, "config_aoe_bearer_token_file", str(token_path))
        aoe_mock.accepted_tokens = {"rev1"}
        await _make_worker(admin, "alice", aoe_session_id="b" * 16)

        await _send_and_wait(admin, "alice")
        token_path.write_text("rev2\n")
        aoe_mock.accepted_tokens = {"rev2"}
        await _send_and_wait(admin, "alice")

        assert len(aoe_mock.sends) == 2
        assert aoe_mock.sends[0][2] == "Bearer rev1"
        assert aoe_mock.sends[1][2] == "Bearer rev2"


async def test_inline_token_takes_precedence_over_file(
    tmp_path, aoe_mock,
) -> None:
    """If both config_aoe_bearer_token AND ..._file are set, the
    inline value wins (explicit-over-implicit). Keeps the existing
    one-secret-key workflow unchanged.
    """
    async with mcp_session(tmp_path) as admin:
        token_path = tmp_path / "serve.token"
        token_path.write_text("from-file\n")
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(admin, "config_aoe_bearer_token", "from-inline")
        _set_ctx(admin, "config_aoe_bearer_token_file", str(token_path))
        aoe_mock.accepted_tokens = {"from-inline"}
        await _make_worker(admin, "alice", aoe_session_id="c" * 16)

        await _send_and_wait(admin, "alice")
        assert aoe_mock.sends[-1][2] == "Bearer from-inline"


async def test_missing_token_file_does_not_crash(tmp_path, aoe_mock) -> None:
    """Path points at a non-existent file → log + skip the send, no
    exception bubbles."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(
            admin,
            "config_aoe_bearer_token_file",
            str(tmp_path / "nope"),
        )
        await _make_worker(admin, "alice", aoe_session_id="d" * 16)

        res = await _send_and_wait(admin, "alice")
        assert "denied" not in res[0].text.lower()
        # We didn't have a usable token → no POST attempted.
        assert aoe_mock.sends == []


# ---------------------------------------------------------------------------
# 5. Health check endpoint
# ---------------------------------------------------------------------------


async def test_aoe_health_ok(tmp_path, aoe_mock) -> None:
    """GET /api/aoe/health hits AoE, reports ok + session count."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(admin, "config_aoe_bearer_token", "good-token")
        aoe_mock.accepted_tokens = {"good-token"}
        aoe_mock.sessions = [
            {"id": "0" * 16, "title": "alice", "status": "Running"},
            {"id": "1" * 16, "title": "bob", "status": "Idle"},
        ]

        r = admin.get("/api/aoe/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["session_count"] == 2


async def test_aoe_health_bad_token(tmp_path, aoe_mock) -> None:
    """If AoE rejects the configured token, health reports unauthorized."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(admin, "config_aoe_bearer_token", "stale-token")
        aoe_mock.accepted_tokens = {"the-current-token"}  # NOT "stale-token"

        r = admin.get("/api/aoe/health")
        assert r.status_code == 200, r.text  # endpoint itself succeeds
        body = r.json()
        assert body["status"] == "unauthorized", body
        # SD-R16-1: the message is sanitised to a static per-status string
        # at the response boundary (no base_url / str(e) / raw upstream
        # text). The coarse "unauthorized" status is the meaningful signal;
        # the message conveys the token was rejected without echoing the
        # upstream response detail.
        assert "reject" in body.get("message", "").lower(), body


async def test_aoe_health_requires_admin(tmp_path) -> None:
    """No token / worker token → 401/403."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/aoe/health")
        assert r.status_code in (401, 403)


async def test_aoe_health_disabled_status(tmp_path, aoe_mock) -> None:
    """When the master toggle is off, health says 'disabled' (don't
    probe AoE at all)."""
    async with mcp_session(tmp_path) as admin:
        # No config_aoe_notify_enabled set (defaults to off).
        r = admin.get("/api/aoe/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "disabled", body
        # No HTTP traffic happened.
        assert aoe_mock.sessions_list_calls == 0
