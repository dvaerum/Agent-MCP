"""Tests for the inbox MCP resource (plan Phase 3).

Per `/home/dennis/.claude/plans/prancy-napping-pie.md` Phase 3:

* `agent-mcp://inbox/<agent_id>` exposes the same event list
  `wait_for_events` returns — JSON object
  `{"events": [...], "next_cursor": "..."}`.
* `resources/list` returns both per-caller URIs (inbox + status).
* `resources/read` on the inbox calls the shared `_collect_events_for`
  helper from Phase 2.

Notification emission (`notifications/resources/updated` on the open
GET /mcp stream) is DEFERRED — see PR body. Stateless StreamableHTTP
mode doesn't expose an enumeration API for in-flight GET sessions,
so cross-request fan-out needs a custom session registry not in
scope for this PR.
"""

from __future__ import annotations

import json
from pathlib import Path

import mcp.types as mcp_types
import pytest

pytestmark = pytest.mark.asyncio


async def _list_resources(session) -> list[mcp_types.Resource]:
    """Call resources/list via the registered MCP handler the same way
    real clients hit it."""
    from agent_mcp.tools.registry import request_auth_token

    handler = session._admin._mcp_app_instance().request_handlers[
        mcp_types.ListResourcesRequest
    ]
    req = mcp_types.ListResourcesRequest(method="resources/list")
    tok = request_auth_token.set(session.token)
    try:
        result = await handler(req)
    finally:
        request_auth_token.reset(tok)
    inner = result.root if hasattr(result, "root") else result
    return list(getattr(inner, "resources", []) or [])


async def _read_resource(session, uri: str) -> mcp_types.ReadResourceResult:
    from agent_mcp.tools.registry import request_auth_token
    from pydantic_core import Url

    handler = session._admin._mcp_app_instance().request_handlers[
        mcp_types.ReadResourceRequest
    ]
    req = mcp_types.ReadResourceRequest(
        method="resources/read",
        params=mcp_types.ReadResourceRequestParams(uri=Url(uri)),
    )
    tok = request_auth_token.set(session.token)
    try:
        result = await handler(req)
    finally:
        request_auth_token.reset(tok)
    inner = result.root if hasattr(result, "root") else result
    return inner


def _first_text(contents) -> str:
    for c in contents:
        text = getattr(c, "text", None)
        if isinstance(text, str):
            return text
    return ""


# ---------------------------------------------------------------------------
# Test 1: resources/list returns both per-caller URIs.
# ---------------------------------------------------------------------------


async def test_resources_list_returns_inbox_and_status(tmp_path: Path) -> None:
    """A worker calling `resources/list` sees their own inbox + status
    URIs, scoped to their agent_id."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        resources = await _list_resources(alice)
        uris = sorted(str(r.uri) for r in resources)
        # Pydantic Url may add a trailing slash; compare on prefix.
        assert any("agent-mcp://inbox/alice" in u for u in uris), (
            f"expected inbox URI in {uris}"
        )
        assert any("agent-mcp://status/alice" in u for u in uris), (
            f"expected status URI in {uris}"
        )


# ---------------------------------------------------------------------------
# Test 2: resources/read on inbox returns the same event envelope shape
# as wait_for_events (both back onto _collect_events_for).
# ---------------------------------------------------------------------------


async def test_inbox_read_returns_event_envelope(tmp_path: Path) -> None:
    """Reading the inbox returns JSON
    `{"events": [...], "next_cursor": "..."}` containing pending
    messages — same shape `wait_for_events` returns."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        await send_agent_message_tool_impl(
            {
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message": "in your inbox",
                "deliver_method": "store",
            }
        )

        result = await _read_resource(alice, "agent-mcp://inbox/alice")
        text = _first_text(result.contents)
        payload = json.loads(text)
        assert "events" in payload, f"missing events; got {payload}"
        assert "next_cursor" in payload, f"missing next_cursor; got {payload}"
        assert len(payload["events"]) == 1, f"want 1 event; got {payload}"
        evt = payload["events"][0]
        assert evt["type"] == "message"
        assert evt["data"]["message_content"] == "in your inbox"
        assert evt["data"]["sender_id"] == "admin"


# ---------------------------------------------------------------------------
# Test 3: caller can't read a different agent's inbox.
# ---------------------------------------------------------------------------


async def test_worker_cannot_read_anothers_inbox(tmp_path: Path) -> None:
    """A worker calling `resources/read` on another agent's inbox
    URI is rejected (or returns an empty/error result) — the
    per-caller URI must enforce the bearer→agent_id binding."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")  # registered as a recipient

        # admin sends a message to bob.
        await send_agent_message_tool_impl(
            {
                "token": admin.admin_token,
                "recipient_id": "bob",
                "message": "for bob only",
                "deliver_method": "store",
            }
        )

        # alice attempts to read bob's inbox. Either the read raises,
        # or returns content whose `events` is empty (the impl
        # rejects the mismatch). Both are acceptable from the
        # contract POV; we assert alice can NOT see bob's message.
        try:
            result = await _read_resource(alice, "agent-mcp://inbox/bob")
        except Exception:
            return  # rejection is also fine

        text = _first_text(result.contents)
        if not text:
            return  # empty content is also fine
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # Non-JSON error response is also acceptable rejection.
            assert "unauth" in text.lower() or "forbid" in text.lower(), (
                f"unexpected non-json response: {text!r}"
            )
            return
        events = payload.get("events", [])
        for e in events:
            assert e.get("data", {}).get("message_content") != (
                "for bob only"
            ), f"alice leaked bob's message: {payload}"
