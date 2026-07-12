"""Agents-of-Empires (AoE) notification side-channel for send_agent_message.

When `config_aoe_notify_enabled=true` is set in project_context,
send_agent_message fires a best-effort HTTP POST to a local AoE
instance so the recipient's tmux pane gets pinged out-of-band. The
message body itself is NEVER forwarded — only `{sender}` and
`{message_id}` substitute into the configured template, to avoid
leaking admin tokens that occasionally appear in message content.

This module is best-effort: failures are logged but never raised, and
the MCP tool call still returns success because the message has
already been persisted to SQLite.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). The `aoe_mock` fixture still
function-scoped and sets `aoe_notify._TRANSPORT_FOR_TESTS` directly;
the harness's mock_ollama transport is unrelated to AoE's dedicated
test hook.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tests.harness import mcp_session, seed_config_context_as_sysadmin


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_ctx(admin, key: str, value: Any) -> None:
    """Upsert a project_context row.

    config_aoe_* is sysadmin-only to write (pentest R8-F1) — the
    operator-tier REST seam below now 403s on it, so seed those keys as
    a sysadmin would. Other keys keep flowing through the REST API.
    """
    if key.lower().startswith("config_aoe_"):
        seed_config_context_as_sysadmin(key, value)
        return
    r = admin.client.post(
        "/api/memories",
        json={
            "token": admin.admin_token,
            "context_key": key,
            "context_value": value,
        },
    )
    if r.status_code == 409:
        r = admin.client.request(
            "PUT",
            f"/api/memories/{key}",
            json={"token": admin.admin_token, "context_value": value},
        )
    assert r.status_code == 200, r.text


async def _send_and_wait(admin, recipient_id: str, body: str = "hi"):
    """Call send_agent_message via the harness and yield to the event
    loop so any `asyncio.create_task` notification fires before we assert.
    """
    result = await admin.call(
        "send_agent_message",
        {
            "recipient_id": recipient_id,
            "message": body,
            "deliver_method": "store",  # no tmux delivery in tests
        },
    )
    # Give the fire-and-forget AoE task a chance to run + any in-flight
    # awaits inside it to complete against the mock transport.
    for _ in range(20):
        await asyncio.sleep(0)
    return result


class _AoeServer:
    """A tiny in-process AoE mock built on httpx.MockTransport.

    Records every request so tests can assert on it. Configure
    `sessions`, `send_status`, `send_status_per_call` to script
    responses; configure `delay_seconds` to simulate a slow AoE.
    """

    def __init__(self) -> None:
        # AoE sessions list. List of dicts with id + title.
        self.sessions: list[dict[str, Any]] = []
        # Default status for POST /api/sessions/<id>/send
        self.send_status: int = 200
        # Per-call queue (overrides default until exhausted)
        self.send_status_per_call: list[int] = []
        # Tracks every send request: list of (aoe_id, body_dict, auth_header)
        self.sends: list[tuple[str, dict, str | None]] = []
        # Tracks every sessions-list request
        self.sessions_list_calls: int = 0
        # Optional artificial latency for /send responses
        self.delay_seconds: float = 0.0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/sessions":
            self.sessions_list_calls += 1
            return httpx.Response(200, json={"sessions": list(self.sessions)})

        if (
            request.method == "POST"
            and path.startswith("/api/sessions/")
            and path.endswith("/send")
        ):
            aoe_id = path[len("/api/sessions/"):-len("/send")]
            body = json.loads(request.read() or b"{}")
            auth = request.headers.get("authorization")
            self.sends.append((aoe_id, body, auth))
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            status = (
                self.send_status_per_call.pop(0)
                if self.send_status_per_call
                else self.send_status
            )
            # AoE returns 404 with a small JSON body when the id is unknown.
            if status == 404:
                return httpx.Response(404, json={"error": "not_found"})
            if status == 200:
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(status, json={"error": "boom"})

        return httpx.Response(404, json={"error": "unhandled"})


@pytest.fixture
def aoe_mock(monkeypatch):
    """Patch the AoE notifier's internal httpx client factory to use a
    MockTransport that records all calls.
    """
    server = _AoeServer()
    transport = httpx.MockTransport(server.handler)

    # Re-import so we patch the live module attribute.
    from agent_mcp.features import aoe_notify

    # Reset module-level state between tests (cache must not leak).
    aoe_notify.clear_session_cache()
    monkeypatch.setattr(
        aoe_notify, "_TRANSPORT_FOR_TESTS", transport, raising=False
    )
    yield server
    aoe_notify.clear_session_cache()


# ---------------------------------------------------------------------------
# Disabled-by-default
# ---------------------------------------------------------------------------


async def test_disabled_by_default_no_aoe_call(tmp_path, aoe_mock) -> None:
    """With no toggle set, send_agent_message does NOT hit AoE."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        res = await _send_and_wait(admin, "alice", "hello")
        text = res[0].text
        assert "denied" not in text.lower()

        assert aoe_mock.sessions_list_calls == 0, (
            f"AoE must not be touched when disabled; got "
            f"{aoe_mock.sessions_list_calls} calls"
        )
        assert aoe_mock.sends == []


async def test_explicit_disable_no_aoe_call(tmp_path, aoe_mock) -> None:
    """Explicit false also disables."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "false")
        await admin.create_worker("alice")

        await _send_and_wait(admin, "alice")
        assert aoe_mock.sends == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_posts_to_aoe(tmp_path, aoe_mock) -> None:
    """Enabled + matching session → POST hits AoE with templated body."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(admin, "config_aoe_bearer_token", "secret-token")
        aoe_mock.sessions = [
            {"id": "abc123def456cafe", "title": "alice", "status": "Running"},
            {"id": "0000000000000000", "title": "bob", "status": "Idle"},
        ]
        await admin.create_worker("alice")

        await _send_and_wait(admin, "alice", "hello")

        assert len(aoe_mock.sends) == 1, aoe_mock.sends
        aoe_id, body, auth = aoe_mock.sends[0]
        assert aoe_id == "abc123def456cafe"
        assert body["revive"] is True
        assert "admin" in body["message"]  # {sender} interpolated
        # Content must NOT be in the typed payload.
        assert "hello" not in body["message"]
        assert auth == "Bearer secret-token"


async def test_cache_is_used_on_second_send(tmp_path, aoe_mock) -> None:
    """Second send to same recipient should hit cached id, not re-list."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        aoe_mock.sessions = [
            {"id": "deadbeefcafebabe", "title": "alice", "status": "Running"},
        ]
        await admin.create_worker("alice")

        await _send_and_wait(admin, "alice")
        await _send_and_wait(admin, "alice")

        assert len(aoe_mock.sends) == 2
        # Only one /api/sessions list call: second was cache hit.
        assert aoe_mock.sessions_list_calls == 1


# ---------------------------------------------------------------------------
# Failure modes (must not break the caller)
# ---------------------------------------------------------------------------


async def test_recipient_not_in_sessions_does_not_error(
    tmp_path, aoe_mock,
) -> None:
    """Recipient title missing from /api/sessions → log + give up."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        aoe_mock.sessions = [
            {"id": "x" * 16, "title": "someone-else", "status": "Running"},
        ]
        await admin.create_worker("alice")

        res = await _send_and_wait(admin, "alice")
        # Caller still sees success — message persisted.
        assert "denied" not in res[0].text.lower()
        # And no POST was issued (we never resolved an id).
        assert aoe_mock.sends == []


async def test_aoe_404_invalidates_cache_and_retries(
    tmp_path, aoe_mock,
) -> None:
    """AoE returns 404 on first send → cache cleared, retry, then give up."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        aoe_mock.sessions = [
            {"id": "ffffffffffffffff", "title": "alice", "status": "Running"},
        ]
        # First send returns 404, second returns 200.
        aoe_mock.send_status_per_call = [404, 200]
        await admin.create_worker("alice")

        await _send_and_wait(admin, "alice")

        # Two sends: original 404 → invalidate cache + re-resolve + retry.
        assert len(aoe_mock.sends) == 2
        # And the sessions list was queried twice (initial + after invalidation).
        assert aoe_mock.sessions_list_calls == 2


async def test_aoe_500_does_not_break_caller(tmp_path, aoe_mock) -> None:
    """AoE returns 500 → log + give up; caller still sees success."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        aoe_mock.sessions = [
            {"id": "a" * 16, "title": "alice", "status": "Running"},
        ]
        aoe_mock.send_status = 500
        await admin.create_worker("alice")

        res = await _send_and_wait(admin, "alice")
        assert "denied" not in res[0].text.lower()
        # POST was attempted once (we don't retry on 5xx).
        assert len(aoe_mock.sends) == 1


async def test_aoe_timeout_does_not_break_caller(tmp_path, aoe_mock) -> None:
    """AoE delays past the timeout → caller still sees success."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        # Very low timeout so the delayed response is treated as a timeout.
        _set_ctx(admin, "config_aoe_timeout_ms", 10)
        aoe_mock.sessions = [
            {"id": "b" * 16, "title": "alice", "status": "Running"},
        ]
        aoe_mock.delay_seconds = 0.5
        await admin.create_worker("alice")

        res = await _send_and_wait(admin, "alice")
        assert "denied" not in res[0].text.lower()


# ---------------------------------------------------------------------------
# Template handling
# ---------------------------------------------------------------------------


async def test_template_substitution(tmp_path, aoe_mock) -> None:
    """{sender} and {message_id} interpolate; literal text passes through."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(
            admin,
            "config_aoe_notify_template",
            "ping {sender} -> {recipient} msg={message_id}",
        )
        aoe_mock.sessions = [
            {"id": "c" * 16, "title": "alice", "status": "Running"},
        ]
        await admin.create_worker("alice")

        await _send_and_wait(admin, "alice")
        assert len(aoe_mock.sends) == 1
        _, body, _ = aoe_mock.sends[0]
        msg = body["message"]
        assert msg.startswith("ping admin -> alice msg=msg_")


async def test_template_rejects_content_placeholder() -> None:
    """validate_template must refuse `{content}` / `{body}` / `{message}` — they
    would leak admin tokens that occasionally appear in message content.

    Pure-Python check — no harness needed. Declared async only so it
    matches the module-level `pytest.mark.asyncio`; no awaits inside.
    """
    from agent_mcp.features.aoe_notify import validate_template

    # These are allowed.
    validate_template("hello {sender}")
    validate_template("msg id {message_id} from {sender} to {recipient}")
    validate_template("no placeholders at all")

    for bad in (
        "you got mail: {content}",
        "see: {body}",
        "got: {message}",
        # Capitalisation should not let you sneak past either.
        "see: {Content}",
    ):
        with pytest.raises(ValueError):
            validate_template(bad)


async def test_invalid_template_is_ignored_and_does_not_error(
    tmp_path, aoe_mock,
) -> None:
    """Bad template in project_context → log warning, no POST, no crash."""
    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_notify_enabled", "true")
        _set_ctx(admin, "config_aoe_base_url", "http://aoe.test")
        _set_ctx(admin, "config_aoe_notify_template", "leaky: {content}")
        aoe_mock.sessions = [
            {"id": "d" * 16, "title": "alice", "status": "Running"},
        ]
        await admin.create_worker("alice")

        res = await _send_and_wait(admin, "alice", "the actual message")
        assert "denied" not in res[0].text.lower()
        # We refused to send because the template was invalid.
        assert aoe_mock.sends == []


# ---------------------------------------------------------------------------
# Secret-key redaction (regression guard)
# ---------------------------------------------------------------------------


async def test_bearer_token_is_redacted_for_workers(tmp_path) -> None:
    """config_aoe_bearer_token must match the secret-key redaction regex so
    workers cannot read it via view_project_context.
    """
    from agent_mcp.tools.project_context_tools import is_secret_key

    assert is_secret_key("config_aoe_bearer_token"), (
        "config_aoe_bearer_token must match the secret-key filter "
        "(see project_context_tools.is_secret_key)"
    )

    async with mcp_session(tmp_path) as admin:
        _set_ctx(admin, "config_aoe_bearer_token", "shhh-secret-aoe-token")

        # Make a worker and call view_project_context via the harness.
        worker = await admin.create_worker("wkr")
        res = await worker.call("view_project_context", {})
        text = res[0].text
        assert "config_aoe_bearer_token" not in text
        assert "shhh-secret-aoe-token" not in text
