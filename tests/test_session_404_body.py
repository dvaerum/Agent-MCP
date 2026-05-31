"""POST /messages/?session_id=<unknown> returns an actionable 404 body,
not just the bare "Could not find session" from the MCP SDK.

Why this matters. Backends lose all in-memory SSE sessions on every
restart (deploy bump, OOM kill, manual restart). An MCP client that
opened its SSE session before the restart keeps POSTing
`/messages/?session_id=<old>` thereafter, and MCP's SDK
(`mcp/server/sse.py:226-227`) returns:

    HTTP/1.1 404 Not Found
    Content-Type: text/plain

    Could not find session

The downstream agent has no idea what to do with that — it doesn't
say "your session was wiped, reconnect", it just says session not
found, which sounds like a configuration bug.

This test pins the friendlier body so a future SDK upgrade or
refactor doesn't silently lose the message.
"""

from __future__ import annotations


def test_unknown_session_404_body_is_actionable(client) -> None:
    """A POST to /messages/?session_id=<random> with no active SSE
    session must respond with HTTP 404 whose body tells the caller
    to restart their MCP connection."""
    r = client.post(
        "/messages/?session_id=00000000000000000000000000000000",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        },
    )
    assert r.status_code == 404, r.status_code
    body = r.text.lower()
    # The original SDK body is just "could not find session". Demand
    # something that points the user at the fix.
    assert "restart" in body or "reconnect" in body or "re-open" in body, (
        "expected the 404 body to instruct the caller to restart / "
        "reconnect their MCP session — without that the error reads "
        "as a configuration bug. Got:\n" + r.text
    )
    # And keep the original phrase so any client that grep'd for it
    # still recognises the condition.
    assert "session" in body, (
        "expected the body to still mention 'session' so clients that "
        "match on the SDK's original phrase still trigger. Got:\n" + r.text
    )


def test_unknown_session_404_body_keeps_session_id(client) -> None:
    """Echo the offending session_id so the operator can grep the
    backend log for the same string. Optional, but cheap."""
    sid = "deadbeefcafef00d0000000000000001"
    r = client.post(
        f"/messages/?session_id={sid}",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert r.status_code == 404, r.status_code
    assert sid in r.text or sid[:8] in r.text, (
        f"expected the session_id ({sid[:8]}…) to appear in the "
        f"response body so operators can correlate with backend logs. "
        f"Got:\n{r.text}"
    )
