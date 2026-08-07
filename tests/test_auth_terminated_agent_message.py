"""Specific 401 error when a bearer token belongs to a terminated agent.

When an agent is terminated, its row stays in the `agents` table with
`status='terminated'` but is removed from the in-memory
`g.active_agents` map. Consequently `verify_token()` (which only
checks `g.active_agents` and the admin token) returns False, and the
`AuthHeaderMiddleware` rejects the request with the same generic 401
it returns for a freshly-invented unknown token.

That's a misleading UX: the token IS valid (it matches a real row),
the agent was just terminated. Claude Code surfaces the generic 401
as "Server rejected the configured Authorization header (HTTP 401).
Check that the token is valid." — but the token *is* valid; the
agent is gone.

This module pins the improved error shape:

  * Bearer belongs to a terminated agent → 401 JSON with
    `error: "agent_terminated"`, `agent_id`, `terminated_at`, and a
    human-readable `message` that names the restore_agent tool.
  * Bearer matches nothing → 401 JSON with `error: "invalid_bearer"`
    and a generic message. (Used to be plain text; the upgrade to
    JSON matches the `serverInfo` error envelope convention.)
  * Bearer matches an active agent → request proceeds normally.

The `AuthHeaderMiddleware` consults the DB (via the new
`query_agent_status` helper in `agent_mcp.core.auth`) only on the
failure path — happy-path requests don't pay a DB round-trip for the
common case of a valid bearer.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _terminate_in_db(agent_id: str, terminated_at: str) -> None:
    """Flip an agent's status to terminated directly in the DB.

    Bypasses the public `terminate_agent` tool path so the test
    controls the exact `terminated_at` ISO string asserted on the
    wire — the tool sets it to `datetime.now().isoformat()` which
    would force a fuzzy match.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET status = ?, terminated_at = ?, "
            "updated_at = ? WHERE agent_id = ?",
            ("terminated", terminated_at, terminated_at, agent_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Mirror what `terminate_agent` does: drop the in-memory entry so
    # the middleware's primary check (verify_token → g.active_agents)
    # correctly returns False, forcing the DB lookup path under test.
    for tok, entry in list(g.active_agents.items()):
        if isinstance(entry, dict) and entry.get("agent_id") == agent_id:
            del g.active_agents[tok]


async def test_terminated_agent_bearer_returns_helpful_401(tmp_path) -> None:
    """A bearer that belongs to a terminated agent → 401 with a JSON
    body naming the agent_id, terminated_at, and restore_agent."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        terminated_at = "2026-06-04T12:34:56"
        _terminate_in_db("alice", terminated_at)

        resp = admin.client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {alice.token}"},
            content=b"",
        )

        assert resp.status_code == 401, resp.text
        ctype = resp.headers.get("content-type", "")
        assert ctype.startswith("application/json"), ctype

        body = resp.json()
        assert body.get("error") == "agent_terminated", body
        assert body.get("agent_id") == "alice", body
        assert body.get("terminated_at") == terminated_at, body
        message = body.get("message", "")
        assert "alice" in message, message
        assert terminated_at in message, message
        assert "restore_agent" in message, message


async def test_unknown_bearer_returns_invalid_bearer_json(tmp_path) -> None:
    """A bearer that matches neither admin nor any agent (active or
    terminated) → 401 JSON with `error: invalid_bearer`. Pins the
    plain-text → JSON upgrade so consumers can rely on a single
    shape."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/mcp",
            headers={"Authorization": "Bearer not-a-real-token-ever"},
            content=b"",
        )

        assert resp.status_code == 401, resp.text
        ctype = resp.headers.get("content-type", "")
        assert ctype.startswith("application/json"), ctype

        body = resp.json()
        assert body.get("error") == "invalid_bearer", body
        message = body.get("message", "")
        assert message, "invalid_bearer body must include a `message` field"


async def test_missing_bearer_still_rejected_as_invalid(tmp_path) -> None:
    """No Authorization header at all → 401 JSON with
    `error: invalid_bearer`. The middleware doesn't distinguish
    "missing" from "wrong" on the failure path — both are 'no valid
    token', and a DB lookup on the empty string returns None."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post("/mcp", content=b"")

        assert resp.status_code == 401, resp.text
        ctype = resp.headers.get("content-type", "")
        assert ctype.startswith("application/json"), ctype

        body = resp.json()
        assert body.get("error") == "invalid_bearer", body


async def test_active_agent_bearer_passes_auth(tmp_path) -> None:
    """A bearer that matches an active agent must NOT trigger the
    new error path. Pins that the middleware doesn't accidentally
    short-circuit valid bearers through the DB lookup branch."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        # POST /mcp with empty body returns 400 (bad request from the
        # MCP transport) or 406 (unsupported Accept), but NOT 401 —
        # auth passed.
        resp = admin.client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {alice.token}"},
            content=b"",
        )

        assert resp.status_code != 401, (
            f"valid active-agent bearer rejected as unauthorized: "
            f"{resp.status_code} {resp.text}"
        )


async def test_query_agent_status_helper_returns_terminated_dict(tmp_path) -> None:
    """Unit test for the new `query_agent_status` helper: it must
    look up by `agents.token` (the bearer) and return the agent's
    status when the row exists. This is what the middleware calls
    on the failure path to decide between `agent_terminated` and
    `invalid_bearer`."""
    from agent_mcp.core.auth import query_agent_status

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        terminated_at = "2026-06-04T01:02:03"
        _terminate_in_db("alice", terminated_at)

        info = query_agent_status(alice.token)
        assert info is not None, "query_agent_status must find the row"
        assert info["agent_id"] == "alice"
        assert info["status"] == "terminated"
        assert info["terminated_at"] == terminated_at


async def test_query_agent_status_returns_none_for_unknown_token(tmp_path) -> None:
    """The helper must return None for a token that doesn't match
    any agent row — that's what tells the middleware to emit
    `invalid_bearer` rather than `agent_terminated`."""
    from agent_mcp.core.auth import query_agent_status

    async with mcp_session(tmp_path):
        info = query_agent_status("definitely-not-an-agent-token")
        assert info is None
