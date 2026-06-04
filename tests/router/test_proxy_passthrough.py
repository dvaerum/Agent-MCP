"""Pure pass-through proxy semantics (regression for commit ce6cd88).

The router used to manually decode and re-encode response chunks
(legacy SSE transport's `data: /messages/` byte rewrite). The fork's
Streamable HTTP migration (PR #61) made that workaround obsolete —
``_proxy_to_backend`` now reads the full upstream body and hands it
back as a single ``web.Response`` so aiohttp emits a clean
Content-Length transfer with no chunked-encoding artifacts.

These tests stand up a tiny aiohttp UDS backend (the same shape the
real agent-mcp@<name>.service runs) and prove:

  1. Request body arrives at the backend byte-for-byte.
  2. Response body comes back byte-for-byte (including a payload
     shaped like SSE events — proves no transport-layer re-encoding).
  3. Headers are passed through with Host/Content-Length stripped on
     the way in and Transfer-Encoding/Content-Length stripped on the
     way out.
  4. The Authorization header is forwarded so the backend's
     AuthHeaderMiddleware sees the bearer token.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = pytest.mark.asyncio


# ── Backend stand-in ────────────────────────────────────────────────


class _FakeBackend:
    """Tiny UDS-bound aiohttp app that records what it received.

    Mounted as a callable handler so tests can dictate the response
    per call. ``records`` holds the (method, path, headers, body) of
    every received request for after-the-fact assertions.
    """

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.response_factory: Callable[[web.Request], Awaitable[web.Response]] | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def _handle(self, req: web.Request) -> web.Response:
        body = await req.read()
        self.records.append(
            {
                "method": req.method,
                "path": req.path,
                "headers": dict(req.headers),
                "body": body,
                "query": dict(req.rel_url.query),
            },
        )
        if self.response_factory is not None:
            return await self.response_factory(req)
        return web.Response(body=b"OK")


async def _start_backend_on_uds(backend: _FakeBackend, sock_path: Path) -> web.AppRunner:
    """Bind ``backend.app()`` to a Unix domain socket at ``sock_path``."""
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def fake_backend(router_module, router_env, systemctl_stub):
    """Stand up a UDS backend at the right path for project 'proj'
    and return the recorder. Project 'proj' is pre-registered and
    the systemctl stub is told to treat the unit as already active.
    """
    name = "proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _FakeBackend()
    runner = await _start_backend_on_uds(backend, sock)
    # Mark the unit "active" so _ensure() doesn't try to start it.
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()


# ── Tests ───────────────────────────────────────────────────────────


async def test_request_body_arrives_byte_for_byte(
    aiohttp_client, router_app, fake_backend, router_module,
) -> None:
    """``POST /agent-mcp/mcp/proj`` body must reach the backend unmodified.

    The MCP /mcp endpoint is auth-gated at the router edge, so we also
    seed the agent-token cache so the bearer check passes without the
    router opening a real /api/tokens connection.
    """
    router_module._agent_token_cache["proj"] = (
        # far-future expiry
        9.9e18,
        {"tok-1234": "Admin"},
    )
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/proj",
        data=payload,
        headers={
            "Authorization": "Bearer tok-1234",
            "Content-Type": "application/json",
        },
    )

    assert resp.status == 200
    assert len(fake_backend.records) == 1
    rec = fake_backend.records[0]
    assert rec["method"] == "POST"
    assert rec["path"] == "/mcp"
    assert rec["body"] == payload, "request body was rewritten in transit"
    # Authorization must be forwarded so the backend can see the
    # bearer token (the fork's AuthHeaderMiddleware needs it).
    assert rec["headers"].get("Authorization") == "Bearer tok-1234"


async def test_response_body_byte_for_byte(
    aiohttp_client, router_app, fake_backend, router_module,
) -> None:
    """Backend response body must reach the client unmodified."""
    router_module._agent_token_cache["proj"] = (
        9.9e18, {"tok-1234": "Admin"},
    )

    response_body = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"x"}]}}'

    async def respond(req):
        return web.Response(body=response_body, content_type="application/json")

    fake_backend.response_factory = respond
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/proj",
        data=b"{}",
        headers={"Authorization": "Bearer tok-1234"},
    )

    assert resp.status == 200
    assert (await resp.read()) == response_body


async def test_sse_shaped_payload_passes_through_cleanly(
    aiohttp_client, router_app, fake_backend, router_module,
) -> None:
    """Regression for ce6cd88: SSE-shaped response body (multiple
    ``data:`` lines separated by blank lines) must arrive at the
    client byte-for-byte, with no chunked-transfer artifacts.

    The router's old hand-rolled byte-pump produced visible re-encoding
    glitches on payloads that looked like SSE events; the pure
    pass-through (``await up.read()`` + ``web.Response``) avoids them.
    """
    router_module._agent_token_cache["proj"] = (
        9.9e18, {"tok-1234": "Admin"},
    )

    sse_payload = (
        b"data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":\"chunk-1\"}\n\n"
        b"data: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":\"chunk-2\"}\n\n"
        b": keep-alive comment\n\n"
        b"data: {\"jsonrpc\":\"2.0\",\"id\":3,\"result\":\"final\"}\n\n"
    )

    async def respond(req):
        # Send with the same content-type the upstream MCP server uses
        # for streaming responses. The router strips Transfer-Encoding
        # and Content-Length on the way out, so we set neither — let
        # aiohttp do its thing.
        return web.Response(body=sse_payload, content_type="text/event-stream")

    fake_backend.response_factory = respond
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/proj",
        data=b"{}",
        headers={"Authorization": "Bearer tok-1234"},
    )

    assert resp.status == 200
    body = await resp.read()
    assert body == sse_payload, (
        "SSE-shaped payload was re-encoded in transit — the pure "
        "pass-through path regressed. See commit ce6cd88."
    )
    # Transfer-Encoding must not be set in the response from the
    # router (the proxy strips it explicitly so aiohttp can pick a
    # clean Content-Length / chunked policy itself).
    assert resp.content_type == "text/event-stream"


async def test_query_string_passes_through(
    aiohttp_client, router_app, fake_backend, router_module,
) -> None:
    """Query parameters on the inbound request must reach the backend."""
    router_module._agent_token_cache["proj"] = (
        9.9e18, {"tok-1234": "Admin"},
    )
    client = await aiohttp_client(router_app)

    await client.post(
        "/agent-mcp/mcp/proj?stream=1&trace=on",
        data=b"{}",
        headers={"Authorization": "Bearer tok-1234"},
    )

    assert fake_backend.records[-1]["query"] == {"stream": "1", "trace": "on"}


async def test_missing_bearer_returns_401(
    aiohttp_client, router_app, fake_backend,
) -> None:
    """No Authorization header → 401 at the router edge, backend
    never sees the request (auth pre-check shifts the failure one
    hop closer to the client)."""
    client = await aiohttp_client(router_app)

    resp = await client.post("/agent-mcp/mcp/proj", data=b"{}")

    assert resp.status == 401
    assert "Bearer" in resp.headers.get("WWW-Authenticate", "")
    assert fake_backend.records == [], (
        "backend should not see a request that fails auth at the "
        "router edge"
    )
