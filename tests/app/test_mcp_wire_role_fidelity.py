"""SEC-1 — role fidelity over the MCP wire (live-reproduced exploit).

Finding 1 (HIGH): the router signed the forwarding header for any
project MEMBER and the backend hard-coded ``project_role="operator"``
for every verified header. A viewer-tier operator therefore collected
the full operator capability bundle over
``POST /mcp`` (agents.register / terminate, tasks.delete,
system.config.write, rag.rebuild …) even though the REST ``/api/``
surface correctly 403'd them.

These tests drive the REAL HTTP path — ``AuthHeaderMiddleware`` verifies
the signed header, builds the ``Principal`` from the SIGNED role, and
the dispatcher gates the tool on the resulting capabilities — so they
pin the fix end-to-end, not just at the Principal-builder unit seam
(that's covered in ``test_mcp_wire_operator_caps.py``).

Also covers the JSON-RPC error-hardening fold-in: a malformed request
must return a terse ``-32600``/``-32602`` envelope, never a pydantic
``ValidationError`` dump.
"""

from __future__ import annotations

import json

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _sign(operator_id: str, role: str) -> dict:
    """Sign a forwarding header for ``operator_id`` at ``role`` against
    the harness's per-test HMAC key."""
    from agent_mcp.app import forwarding_header as _fh
    from agent_mcp.core import globals as g

    assert g.forwarding_hmac_key, "harness should have stamped an HMAC key"
    return {_fh.HEADER_NAME: _fh.sign(operator_id, role, g.forwarding_hmac_key)}


def _jsonrpc_result_from_sse(body: str) -> dict:
    """Extract the first JSON-RPC payload from a text/event-stream body.

    The stateless StreamableHTTP transport frames the tools/call reply
    as ``event: message`` + ``data: <json-rpc>``; pull the ``data:``
    line and parse it."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"no SSE data frame in response body: {body!r}")


def _tools_call(client, tool_name: str, arguments: dict, headers: dict):
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


# ── Finding 1: viewer over the wire ─────────────────────────────────


async def test_viewer_forwarding_header_denied_register_agent(tmp_path) -> None:
    """A viewer-signed forwarding header calling ``register_agent`` over
    ``POST /mcp`` is DENIED — the escalation is closed.

    Pre-SEC-1 this returned a live-minted agent bearer (200 + success):
    the backend collapsed the viewer's role to ``operator`` and handed
    over ``agents.register``. Now the signed ``viewer`` role rides the
    header, ``resolve_capabilities`` yields the viewer bundle, and the
    dispatcher rejects the admin tool."""
    async with mcp_session(tmp_path) as admin:
        r = _tools_call(
            admin.client,
            "register_agent",
            {"name": "sec1-escalation-probe", "role": "worker"},
            _sign("viewer-op", "viewer"),
        )
        assert r.status_code == 200, r.text
        payload = _jsonrpc_result_from_sse(r.text)
        result = payload.get("result", {})
        text = " ".join(
            block.get("text", "")
            for block in result.get("content", [])
            if isinstance(block, dict)
        ).lower()
        # The wire surfaces an authorization denial as an "Unauthorized:
        # …" text block (the tool's ``_require_capability`` denial),
        # matching the harness's ``assert_unauthorized`` contract.
        assert "unauthorized" in text or "permission" in text, (
            f"viewer register_agent should be denied for lack of the "
            f"agents.register capability; got {payload!r}"
        )


async def test_viewer_forwarding_header_allowed_read(tmp_path) -> None:
    """The SAME viewer header is still admitted for a viewer-tier READ
    (``view_tasks`` needs only ``tasks.view``, which is in the viewer
    bundle). Proves the fix denies WRITE without breaking viewer READ —
    it's a role-fidelity fix, not a blanket lockout.

    Uses ``view_tasks`` rather than ``view_status``: the viewer-read-
    gating fix (2026-07-08, finding 1) moved ``view_status`` /
    ``view_audit_log`` off the viewer-held ``system.view`` onto the
    operator-only ``system.config.write`` (they leak agent working
    dirs + operator user_ids), so ``view_status`` is no longer a
    viewer read. ``view_tasks`` is the genuine viewer read that keeps
    this test asserting what it means to."""
    async with mcp_session(tmp_path) as admin:
        r = _tools_call(
            admin.client,
            "view_tasks",
            {},
            _sign("viewer-op", "viewer"),
        )
        assert r.status_code == 200, r.text
        payload = _jsonrpc_result_from_sse(r.text)
        result = payload.get("result", {})
        text = " ".join(
            block.get("text", "")
            for block in result.get("content", [])
            if isinstance(block, dict)
        ).lower()
        assert result.get("isError") is not True, (
            f"viewer view_tasks (a read) must succeed; got {payload!r}"
        )
        assert "unauthorized" not in text, (
            f"viewer view_tasks (tasks.view is a viewer cap) must NOT be "
            f"denied; got {payload!r}"
        )


async def test_operator_forwarding_header_allowed_register_agent(tmp_path) -> None:
    """Regression guard: an operator-signed header still gets the
    operator bundle — ``register_agent`` succeeds over the wire. Keeps
    the operator path (the legitimate use of the forwarding header)
    working after the fix."""
    async with mcp_session(tmp_path) as admin:
        r = _tools_call(
            admin.client,
            "register_agent",
            {"name": "sec1-operator-agent", "role": "worker"},
            _sign("operator-op", "operator"),
        )
        assert r.status_code == 200, r.text
        payload = _jsonrpc_result_from_sse(r.text)
        result = payload.get("result", {})
        text = " ".join(
            block.get("text", "")
            for block in result.get("content", [])
            if isinstance(block, dict)
        ).lower()
        assert "unauthorized" not in text, (
            f"operator register_agent must NOT be denied; got {payload!r}"
        )
        assert result.get("isError") is not True, (
            f"operator register_agent must succeed; got {payload!r}"
        )


async def test_unknown_role_forwarding_header_rejected(tmp_path) -> None:
    """A forwarding header claiming an unknown role (hand-forged with a
    valid HMAC over ``role='sysadmin'``) is rejected at the middleware
    with 401 — an unrecognised tier must never be admitted."""
    async with mcp_session(tmp_path) as admin:
        import time as _time

        from agent_mcp.app import forwarding_header as _fh
        from agent_mcp.core import globals as g

        # Sign the exact bytes with an unknown role — the HMAC is valid
        # (fresh, within the replay window), so the ONLY reason to
        # reject is the unknown-role guard.
        expiry = int(_time.time()) + 20
        mac = _fh._hmac_hex("attacker", "sysadmin", expiry, g.forwarding_hmac_key)
        forged = f"attacker.sysadmin.{expiry}.{mac}"
        r = admin.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={**_MCP_HEADERS, _fh.HEADER_NAME: forged},
        )
        assert r.status_code == 401, r.text


# ── Fold-in: JSON-RPC error hardening ───────────────────────────────


async def test_malformed_jsonrpc_returns_terse_error(tmp_path) -> None:
    """A structurally-invalid JSON-RPC POST (``method`` as an int,
    ``params`` as a string) returns a terse ``-32600``/``-32602``
    envelope — NOT the SDK's raw pydantic ``ValidationError`` dump.

    Pre-fix the error ``message`` leaked ``input_value=…``,
    ``errors.pydantic.dev`` URLs and the internal model field names,
    disclosing the server's library + schema shape."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": 12345,
                "params": "notanobject",
            },
            headers={
                **_MCP_HEADERS,
                "Authorization": f"Bearer {admin.admin_token}",
            },
        )
        assert r.status_code == 400, r.text
        body = r.text
        lowered = body.lower()
        assert "pydantic" not in lowered, body
        assert "input_value" not in lowered, body
        assert "validation error" not in lowered, body
        payload = json.loads(body)
        assert payload["error"]["code"] in (-32600, -32602), payload
        # The terse message carries no internal detail.
        assert payload["error"]["message"] in ("Invalid Request", "Invalid params")
