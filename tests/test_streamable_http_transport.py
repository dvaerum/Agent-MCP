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
"""

from __future__ import annotations

import json


# ---------- helpers --------------------------------------------------


def _admin_token(client) -> str:
    return client.get("/api/tokens").json()["admin_token"]


def _post_mcp(client, body: dict, headers: dict | None = None):
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        hdrs.update(headers)
    return client.post("/mcp", json=body, headers=hdrs)


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


def test_post_mcp_with_admin_bearer_returns_tools_list(client) -> None:
    """The new transport must accept POST /mcp with a JSON-RPC body and
    return tools/list inline."""
    admin = _admin_token(client)
    r = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert r.status_code == 200, r.text
    payload = _extract_jsonrpc_result(r)
    assert payload.get("jsonrpc") == "2.0", payload
    assert payload.get("id") == 1, payload
    assert "result" in payload, payload
    tools = payload["result"].get("tools")
    assert isinstance(tools, list) and len(tools) > 0, payload


def test_post_mcp_with_bad_bearer_returns_401(client) -> None:
    """Bad token at the HTTP layer → 401 from the auth middleware
    before tool dispatch runs."""
    r = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Authorization": "Bearer " + ("x" * 32)},
    )
    assert r.status_code == 401, r.text


def test_post_mcp_without_authorization_returns_401(client) -> None:
    """No Authorization header → 401."""
    r = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert r.status_code == 401, r.text


# ---------- GET /mcp -------------------------------------------------


def test_get_mcp_is_routed_to_streamable_http_manager(app) -> None:
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
    from starlette.routing import Mount
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from agent_mcp.app.main_app import _McpAsgiApp

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


def test_delete_mcp_returns_405_in_stateless_mode(client) -> None:
    """In stateless mode there is no session to terminate, so DELETE
    /mcp must return 405 Method Not Allowed per the spec."""
    admin = _admin_token(client)
    r = client.request(
        "DELETE",
        "/mcp",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert r.status_code == 405, r.text


# ---------- Legacy endpoints return 410 -----------------------------


def _assert_migration_body(body_text: str) -> None:
    data = json.loads(body_text)
    assert data.get("error") == "endpoint_removed", data
    assert data.get("migrated_to") == "/mcp", data
    assert data.get("spec_revision") == "2025-03-26", data
    assert "hint" in data, data


def test_legacy_sse_endpoint_returns_410(client) -> None:
    r = client.get("/sse")
    assert r.status_code == 410, r.text
    _assert_migration_body(r.text)


def test_legacy_messages_endpoint_returns_410(client) -> None:
    r = client.post(
        "/messages/?session_id=00000000000000000000000000000000",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert r.status_code == 410, r.text
    _assert_migration_body(r.text)


# ---------- Restart safety -------------------------------------------


def test_post_mcp_works_after_session_state_cleared(client) -> None:
    """The point of going stateless: nuking whatever the
    SessionManager has cached mid-process must not break the next POST.
    No handshake re-required."""
    admin = _admin_token(client)

    # Warm up with one successful POST so any per-request internals get
    # exercised first.
    r1 = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {admin}"},
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
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert r2.status_code == 200, r2.text
    payload = _extract_jsonrpc_result(r2)
    assert payload.get("id") == 2, payload
    assert "result" in payload, payload
