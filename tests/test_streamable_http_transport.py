"""Streamable HTTP transport per MCP spec rev 2025-03-26.

Replaces the legacy `/sse` + `/messages/` paired-endpoint SSE transport
with a single `POST/GET/DELETE /mcp` endpoint backed by upstream's
`mcp.server.streamable_http_manager.StreamableHTTPSessionManager` in
*stateless* mode. The whole point: no per-session in-memory dict that
dies on backend restart.

What this test pins:

* `POST /mcp` with `Authorization: Bearer <admin>` and a JSON-RPC body
  returns the response inline (either application/json or
  text/event-stream depending on whether the tool emits progress).
* `POST /mcp` without (or with an invalid) bearer fails with 401 — the
  same `AuthHeaderMiddleware` that fronts the rest of the app must apply
  here too, so unauthenticated requests never reach tool dispatch.
* `GET /mcp` opens a long-lived SSE stream the spec uses for
  server-initiated notifications.
* `DELETE /mcp` → 405 (no session state to clean up in stateless mode).
* The legacy `/sse` and `/messages/?session_id=...` endpoints return
  410 Gone with a JSON body pointing operators at the new endpoint.
* Restart safety: tear down all SessionManager state mid-process and
  the next POST still succeeds with no handshake.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). Where the legacy fixture exposed an
`app` Starlette instance for route inspection, the harness exposes
the same instance via `admin.client.app`.
"""

from __future__ import annotations

import json

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ---------- helpers --------------------------------------------------


def _post_mcp(client, body: dict, headers: dict | None = None):
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        hdrs.update(headers)
    return client.post("/mcp", json=body, headers=hdrs)


def _post_mcp_raw(client, raw: bytes, headers: dict | None = None):
    """POST a raw, already-encoded JSON-RPC body.

    Used instead of `_post_mcp` (which round-trips the body through
    `json.dumps` on the *client* side too) for the deep-nesting tests
    below: building the nested payload as a real Python `list` and
    handing it to httpx's `json=` kwarg would itself blow the client's
    recursion limit while *encoding* it. Building the raw bytes by
    string concatenation sidesteps that — the depth only matters to the
    *server's* `json.loads`, which is exactly what's under test.
    """
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        hdrs.update(headers)
    return client.post("/mcp", content=raw, headers=hdrs)


def _nested_array_body(depth: int, *, request_id: int = 1) -> bytes:
    """Build a JSON-RPC `tools/call` body whose `arguments` field is an
    array nested `depth` levels deep, as raw bytes (see `_post_mcp_raw`)."""
    nested = ("[" * depth) + "1" + ("]" * depth)
    return (
        '{"jsonrpc":"2.0","id":'
        + str(request_id)
        + ',"method":"tools/call","params":{"name":"x","arguments":'
        + nested
        + "}}"
    ).encode("utf-8")


def _parse_sse_payload(body_text: str) -> dict:
    """Parse the first SSE `data:` frame body as JSON."""
    for line in body_text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"no `data:` frame in SSE body: {body_text!r}")


def _extract_jsonrpc_result(response) -> dict:
    """Decode whichever response shape (`application/json` or
    `text/event-stream`) the transport chose for this call."""
    ctype = response.headers.get("content-type", "")
    if "application/json" in ctype:
        return response.json()
    if "text/event-stream" in ctype:
        return _parse_sse_payload(response.text)
    raise AssertionError(
        f"unexpected content-type {ctype!r} body={response.text!r}"
    )


# ---------- POST /mcp ------------------------------------------------


async def test_post_mcp_with_admin_bearer_returns_tools_list(tmp_path) -> None:
    """The new transport must accept POST /mcp with a JSON-RPC body and
    return tools/list inline."""
    async with mcp_session(tmp_path) as admin:
        r = _post_mcp(
            admin.client,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        payload = _extract_jsonrpc_result(r)
        assert payload.get("jsonrpc") == "2.0", payload
        assert payload.get("id") == 1, payload
        assert "result" in payload, payload
        tools = payload["result"].get("tools")
        assert isinstance(tools, list) and len(tools) > 0, payload


async def test_post_mcp_with_bad_bearer_returns_401(tmp_path) -> None:
    """Bad token at the HTTP layer → 401 from the auth middleware
    before tool dispatch runs."""
    async with mcp_session(tmp_path) as admin:
        r = _post_mcp(
            admin.client,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": "Bearer " + ("x" * 32)},
        )
        assert r.status_code == 401, r.text


async def test_post_mcp_without_authorization_returns_401(tmp_path) -> None:
    """No Authorization header → 401."""
    async with mcp_session(tmp_path) as admin:
        r = _post_mcp(
            admin.client,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r.status_code == 401, r.text


# ---------- pentest R1-F2: pre-parse recursion-depth guard -----------
#
# The MCP SDK's own `streamable_http.py::_handle_post_request` wraps its
# `json.loads(body)` in `except json.JSONDecodeError` only — a deeply
# nested body blows Python's recursion limit and raises `RecursionError`
# (a `RuntimeError`, NOT a `JSONDecodeError`/`ValueError` subclass), which
# escapes uncaught and surfaces as HTTP 500. The identical class of input
# hits every REST/router JSON-parse surface's `except (ValueError,
# RecursionError)` guard and returns a clean 400 instead. These tests pin
# the `_McpAsgiApp.__call__` pre-parse guard (`_drain_body` +
# `_MCP_DEPTH_GUARD_BODY` in `agent_mcp/app/main_app.py`) that closes that
# gap — the last unguarded JSON-parse surface in the codebase.


async def test_post_mcp_deeply_nested_body_returns_400_not_500(tmp_path) -> None:
    """A body nested far past Python's recursion limit must be rejected
    with a clean 400 JSON-RPC parse error, not crash the SDK's parser
    into an HTTP 500 (pentest R1-F2)."""
    async with mcp_session(tmp_path) as admin:
        r = _post_mcp_raw(
            admin.client,
            _nested_array_body(50_000),
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 400, r.text
        payload = r.json()
        assert payload.get("jsonrpc") == "2.0", payload
        assert payload["error"]["code"] == -32700, payload
        assert payload["error"]["message"] == "Parse error", payload
        # No recursion-limit / interpreter detail in the response body.
        assert "recursion" not in r.text.lower()


async def test_post_mcp_under_limit_nested_body_is_accepted(tmp_path) -> None:
    """A legitimately-nested-but-under-the-limit body must NOT be
    rejected by the depth guard — only a body that actually blows the
    recursion limit should trip it."""
    async with mcp_session(tmp_path) as admin:
        r = _post_mcp_raw(
            admin.client,
            _nested_array_body(100),
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        # The tool ("x") doesn't exist, so this may come back as an
        # in-band JSON-RPC error — the point under test is that it is
        # NOT rejected by the depth guard (400 with code -32700) and did
        # NOT crash the server (500).
        assert r.status_code not in (400, 500), r.text
        payload = _extract_jsonrpc_result(r)
        assert payload.get("id") == 1, payload


async def test_post_mcp_normal_depth_body_still_works(tmp_path) -> None:
    """No-regression check: an ordinary, shallow JSON-RPC body posted as
    raw bytes through the same code path as the two tests above must
    still round-trip normally."""
    async with mcp_session(tmp_path) as admin:
        r = _post_mcp_raw(
            admin.client,
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        payload = _extract_jsonrpc_result(r)
        assert payload.get("id") == 1, payload
        assert "result" in payload, payload
        tools = payload["result"].get("tools")
        assert isinstance(tools, list) and len(tools) > 0, payload


# ---------- GET /mcp -------------------------------------------------


async def test_get_mcp_is_routed_to_streamable_http_manager(tmp_path) -> None:
    """GET /mcp must reach the StreamableHTTP session manager.

    We can't easily exercise the live SSE stream with Starlette's
    TestClient — the stream is long-lived (server-push channel per
    spec) and the in-process EventSourceResponse blocks lifespan
    teardown if the test holds the connection. The wire-level shape
    (200 + text/event-stream content-type, with the manager handling
    GET as the spec's server-push channel) is covered by upstream's
    own test suite. The integration concern here is just that we
    didn't accidentally drop GET routing when we wired the Mount.

    Assert by route inspection: `/mcp` is a Mount whose inner app is
    our `_McpAsgiApp` wrapper, and the wrapper holds a real
    `StreamableHTTPSessionManager` instance (not `None`).
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.routing import Mount

    from agent_mcp.app.main_app import _McpAsgiApp

    async with mcp_session(tmp_path) as admin:
        app = admin.client.app
        mcp_mount = next(
            (r for r in app.routes if isinstance(r, Mount) and r.path == "/mcp"),
            None,
        )
        assert mcp_mount is not None, (
            "expected a Mount('/mcp', ...) for the Streamable HTTP transport"
        )
        inner = mcp_mount.app
        assert isinstance(inner, _McpAsgiApp), (
            f"/mcp Mount should be an _McpAsgiApp; got {type(inner).__name__}"
        )
        assert isinstance(inner._manager, StreamableHTTPSessionManager), (
            f"/mcp ASGI wrapper must hold a StreamableHTTPSessionManager; "
            f"got {type(inner._manager).__name__}"
        )
        # Stateless mode is the whole point of the rewrite — assert it
        # explicitly so a future regression that flips to stateful mode
        # (and thus reintroduces the lost-session-on-restart bug) fails
        # loudly here.
        assert inner._manager.stateless is True, (
            "Streamable HTTP transport must be in stateless mode so backend "
            "restarts don't lose session state"
        )


# ---------- DELETE /mcp ---------------------------------------------


async def test_delete_mcp_returns_405_in_stateless_mode(tmp_path) -> None:
    """In stateless mode there is no session to terminate, so DELETE
    /mcp must return 405 Method Not Allowed per the spec."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.request(
            "DELETE",
            "/mcp",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 405, r.text


# ---------- Legacy endpoints return 410 -----------------------------


def _assert_migration_body(body_text: str) -> None:
    data = json.loads(body_text)
    assert data.get("error") == "endpoint_removed", data
    assert data.get("migrated_to") == "/mcp", data
    assert data.get("spec_revision") == "2025-03-26", data
    assert "hint" in data, data


async def test_legacy_sse_endpoint_returns_410(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/sse")
        assert r.status_code == 410, r.text
        _assert_migration_body(r.text)


async def test_legacy_messages_endpoint_returns_410(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/messages/?session_id=00000000000000000000000000000000",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r.status_code == 410, r.text
        _assert_migration_body(r.text)


# ---------- Restart safety -------------------------------------------


async def test_post_mcp_works_after_session_state_cleared(tmp_path) -> None:
    """The point of going stateless: nuking whatever the
    SessionManager has cached mid-process must not break the next POST.
    No handshake re-required."""
    async with mcp_session(tmp_path) as admin:
        # Warm up with one successful POST so any per-request internals get
        # exercised first.
        r1 = _post_mcp(
            admin.client,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r1.status_code == 200, r1.text

        # Reach into the StreamableHTTPSessionManager and clear whatever
        # session-ish bookkeeping it has. In stateless mode the
        # `_server_instances` dict is unused but we clear it anyway to make
        # the invariant explicit and resilient if upstream ever populates it.
        from agent_mcp.app import main_app

        sm = getattr(main_app, "session_manager", None)
        assert sm is not None, (
            "expected agent_mcp.app.main_app.session_manager to be a "
            "StreamableHTTPSessionManager instance"
        )
        if hasattr(sm, "_server_instances"):
            sm._server_instances.clear()
        if hasattr(sm, "_session_owners"):
            sm._session_owners.clear()

        r2 = _post_mcp(
            admin.client,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r2.status_code == 200, r2.text
        payload = _extract_jsonrpc_result(r2)
        assert payload.get("id") == 2, payload
        assert "result" in payload, payload
