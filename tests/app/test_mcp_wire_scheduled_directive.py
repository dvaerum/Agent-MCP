"""MCP-wire E2E — scheduled directives over the real POST /mcp path.

Drives the full wire (AuthHeaderMiddleware → JSON-RPC framing →
dispatcher → capability gate → tool impl): a worker bearer registers a
schedule via ``create_scheduled_directive`` and receives the ``directive``
event on its ``wait_for_events`` loop.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _jsonrpc_result_from_sse(body: str) -> dict:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"no SSE data frame in response body: {body!r}")


def _tools_call(client, tool_name, arguments, headers):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers={**_MCP_HEADERS, **headers},
    )


def _result_payload(r) -> dict:
    assert r.status_code == 200, r.text
    payload = _jsonrpc_result_from_sse(r.text)
    return payload["result"]


def _result_data(result: dict) -> dict:
    """Parse the tool's JSON data payload from the tools/call result.

    An ``Ok(message=..., data=...)`` renders as two text blocks
    ``[message, json(data)]``; the data (last block) is the JSON payload.
    """
    blocks = [
        b.get("text", "")
        for b in result.get("content", [])
        if isinstance(b, dict)
    ]
    return json.loads(blocks[-1])


async def test_worker_creates_schedule_and_receives_directive(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        hdr = {"Authorization": f"Bearer {alice.token}"}

        # 1. Create a schedule with an immediate first fire.
        r = _tools_call(
            admin.client,
            "create_scheduled_directive",
            {
                "prompt": "report the CI status",
                "interval_seconds": 60,
                "run_now": True,
            },
            hdr,
        )
        result = _result_payload(r)
        assert not result.get("isError"), result
        body = _result_data(result)
        assert body["directive"]["agent_id"] == "alice"
        assert body["directive"]["enabled"] is True

        # 2. The loop check-in fires the run_now schedule immediately.
        since = (_dt.datetime.now() - _dt.timedelta(seconds=1)).isoformat()
        r2 = _tools_call(
            admin.client,
            "wait_for_events",
            {"since": since, "timeout_seconds": 5},
            hdr,
        )
        env = _result_data(_result_payload(r2))
        directives = [e for e in env["events"] if e["type"] == "directive"]
        assert directives, env
        assert directives[0]["data"]["source"] == "schedule"
        assert directives[0]["data"]["prompt"] == "report the CI status"


async def test_worker_lists_and_deletes_schedule_over_wire(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        hdr = {"Authorization": f"Bearer {alice.token}"}

        created = _result_data(
            _result_payload(
                _tools_call(
                    admin.client,
                    "create_scheduled_directive",
                    {"prompt": "x", "interval_seconds": 90},
                    hdr,
                )
            )
        )
        did = created["directive"]["directive_id"]

        listing = _result_data(
            _result_payload(
                _tools_call(admin.client, "list_scheduled_directives", {}, hdr)
            )
        )
        assert listing["count"] == 1
        assert listing["directives"][0]["directive_id"] == did

        _result_payload(
            _tools_call(
                admin.client,
                "delete_scheduled_directive",
                {"directive_id": did},
                hdr,
            )
        )
        listing2 = _result_data(
            _result_payload(
                _tools_call(admin.client, "list_scheduled_directives", {}, hdr)
            )
        )
        assert listing2["count"] == 0
