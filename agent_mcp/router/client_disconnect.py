"""Peer-disconnect vocabulary shared by the router's request paths.

A client that goes away mid-request is the NORMAL termination of a
long-lived stream (a dashboard tab closing its ``operator_events`` SSE,
an aborted fetch, an agent restarting its ``GET /mcp`` notification
channel), not an application fault. aiohttp signals it as
``ClientConnectionResetError("Cannot write to closing transport")`` on
the write side and ``ConnectionResetError("Connection lost")`` on the
read side — both ``ConnectionError`` subclasses.

Left unhandled, either escapes the web handler and aiohttp renders it
through ``RequestHandler.handle_error``: a 500 nobody can receive plus a
full ``aiohttp.server`` ERROR traceback in the journal. That traceback is
indistinguishable at a glance from a genuine failure, so it costs a
triage every time and masks the real errors around it. This module holds
the two primitives used to recognise and answer that case consistently
wherever the router touches a client connection.

Deliberately NOT a blanket handler: :func:`client_is_gone` checks the
DOWNSTREAM transport, so a ``ConnectionError`` raised while the peer is
still connected — an upstream/backend reset, say — stays a real error and
keeps its traceback.

Layering, which is what keeps that property true: the quiet path belongs
at the DOWNSTREAM WRITE SITE (``app._stream_upstream_to_client``,
``app._proxy_to_backend``'s request-body read), where "this write to this
client failed" is unambiguous evidence the client is gone and no second
signal is needed. The middleware's use of :func:`client_is_gone` is the
last-resort net for whatever reaches it anyway — never the place to
widen, because a generic handler cannot tell a failed write to the client
from a failed read off the backend.
"""

from __future__ import annotations

from aiohttp import web

# nginx's non-standard "Client Closed Request". Never reaches a wire —
# by definition the peer is gone — but it keeps the access log honest
# (and distinct from a 200 or a 500) about how the request ended.
CLIENT_GONE_STATUS = 499


def client_is_gone(request: web.Request) -> bool:
    """True when ``request``'s downstream transport is closed/closing.

    aiohttp drops the protocol's transport on ``connection_lost`` and
    marks it closing on a FIN/RST, so this is the authoritative "is the
    peer still there" read at any point during a handler.
    """
    transport = request.transport
    return transport is None or transport.is_closing()


def client_gone_response() -> web.Response:
    """The response to return once the peer has provably gone away."""
    return web.Response(status=CLIENT_GONE_STATUS)


__all__ = ["CLIENT_GONE_STATUS", "client_gone_response", "client_is_gone"]
