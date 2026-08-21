"""R7-F2: aiohttp's protocol-parser error path leaks the raw
``Server: Python/x.y aiohttp/z`` banner.

``security_headers_middleware`` hardens every response that flows
through ``web.Application.__call__`` — but a genuinely malformed
HTTP/1.1 request (non-numeric ``Content-Length``, duplicate
``Content-Length``, a bad chunk size, ...) never reaches that layer
at all: aiohttp's own ``RequestHandler.handle_error`` (in
``web_protocol.py``) builds a bare ``Response(...)`` and returns it
directly from ``data_received``, bypassing ``web.Application`` — and
therefore every middleware, including this one — entirely. That bare
``Response.write_headers()`` fills in ``Server`` via
``headers.setdefault(hdrs.SERVER, SERVER_SOFTWARE)``, disclosing the
exact aiohttp + Python version to any client that trips the parser
(confirmed live against the vm-dev sandbox with
``printf 'GET / HTTP/1.1\\r\\nHost: x\\r\\nContent-Length: abc\\r\\n
Connection: close\\r\\n\\r\\n' | nc``).

The only place this is fixable is aiohttp's own default, so
``agent_mcp.router.security_headers`` reassigns the module-level
``SERVER_SOFTWARE`` constant (both the ``aiohttp.http`` binding and
the separate ``aiohttp.web_response`` name — the latter binds its
own copy at import time via ``from .http import SERVER_SOFTWARE``,
so patching only one leaves the other holding the real banner) to
the same neutral ``_SERVER_BANNER`` the middleware itself uses, as a
MODULE-LEVEL side effect of importing ``security_headers``. Both
router entrypoints (``router/app.py``'s ``make_app`` and
``cli.py``'s router subcommand) import that module before
``web.run_app`` is ever called, so the reassignment is guaranteed to
land before the first request is processed — including one that
never touches ``web.Application`` or any middleware at all.

This test drives malformed HTTP/1.1 at the RAW SOCKET level — an
``aiohttp_client``-level test can't construct a genuinely malformed
request, since the client's own encoder refuses invalid input before
it ever reaches the wire.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

# Import triggers the module-level ``SERVER_SOFTWARE`` reassignment
# under test — mirrors how both real entrypoints (``make_app`` /
# the ``router`` CLI subcommand) pull this module in before
# ``web.run_app`` is ever called. The name itself is unused: the
# bare ``web.Application`` built below deliberately does NOT wire in
# ``security_headers_middleware``, so this import's side effect is
# the ONLY thing that can make the assertion below pass — isolating
# the process-wide constant fix from the middleware's own per-response
# ``Server`` overwrite (already covered by
# ``test_sec_r5_headers_500_bypass.py``).
from agent_mcp.router.security_headers import (  # noqa: F401
    security_headers_middleware as _unused_import_side_effect,
)

pytestmark = pytest.mark.asyncio


async def _send_raw_and_read_response(host: str, port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(65536), timeout=5.0)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _server_header(raw_response: bytes) -> str:
    head, _, _ = raw_response.partition(b"\r\n\r\n")
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"server:"):
            return line.split(b":", 1)[1].strip().decode("latin-1")
    return ""


async def test_malformed_content_length_does_not_leak_aiohttp_banner() -> None:
    """A request with a non-numeric ``Content-Length`` trips aiohttp's
    raw HTTP/1.1 parser BEFORE ``web.Application`` (and every
    middleware) ever sees it. The resulting 400 must not carry
    aiohttp's raw version-disclosing ``Server`` banner."""

    async def handler(request: web.Request) -> web.Response:  # pragma: no cover
        return web.Response(text="ok")

    # Deliberately a BARE app with no security_headers_middleware
    # wired in — the malformed request never reaches middleware
    # dispatch at all, so a passing assertion here can only be
    # explained by the process-wide SERVER_SOFTWARE reassignment
    # (triggered by the module import above), not by
    # ``_apply_headers``'s per-response ``Server`` overwrite.
    app = web.Application()
    app.router.add_get("/", handler)

    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        raw_request = (
            b"GET / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: abc\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        raw_response = await _send_raw_and_read_response(
            server.host, server.port, raw_request,
        )
    finally:
        await server.close()

    status_line = raw_response.split(b"\r\n", 1)[0]
    assert b"400" in status_line, raw_response[:200]
    server_header = _server_header(raw_response)
    assert server_header, raw_response[:200]
    assert "aiohttp" not in server_header.lower(), server_header
    assert "python" not in server_header.lower(), server_header
