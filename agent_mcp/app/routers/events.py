"""Operator dashboard live-update SSE channel — ``GET /api/events``.

The dashboard's notification client opens a long-lived
``text/event-stream`` here and refetches its pages whenever the backend
signals that project data changed. Every mutation logs an action at the
``log_agent_action_to_db`` choke point, which publishes a
``notifications/resources/updated`` envelope onto the in-process
``features.operator_events`` hub; this endpoint drains that hub onto the
wire.

Why a dedicated endpoint (not the ``/mcp`` transport): the MCP
StreamableHTTP transport's GET stream needs an ``Mcp-Session-Id`` from a
prior per-agent ``initialize`` handshake the dashboard never performs,
and its ``agent_id`` derives from a per-agent bearer the operator cookie
can't carry — so a cookie-only dashboard GET got a 405. Operators are
not agents; the agent-scoped ``session_registry`` (``agent_id`` FK into
``agents``) can't hold them. This endpoint authenticates the operator
session cookie via ``require_operator_session`` and subscribes to the
operator-side hub instead.

Final URL: backend ``/api/events`` → dashboard
``/agent-mcp/api/<project>/events`` (the router's ``/api/...`` proxy
streams the response body chunk-by-chunk, so the SSE frames flow through
untouched).
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..deps import require_operator_session
from ...features import operator_events


router = APIRouter(prefix="/api", tags=["events"])


# Idle interval between heartbeat comment frames. Mirrors the GET /mcp
# pump's heartbeat cadence: a comment every ~20s keeps intermediaries
# from reaping an idle stream and doubles as a dead-peer detector (a
# write to a closed socket raises, ending the generator).
HEARTBEAT_SECONDS = 20


@router.get("/events")
async def operator_events_stream(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> StreamingResponse:
    """Stream ``notifications/resources/updated`` envelopes to an
    authenticated operator as Server-Sent Events."""

    async def gen():
        q = operator_events.subscribe()
        try:
            # Initial comment frame so headers flush immediately — the
            # client knows the connection is live without waiting for
            # the first real notification.
            yield b": connected\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(
                        q.get(), timeout=HEARTBEAT_SECONDS
                    )
                    yield ("data: " + json.dumps(item) + "\n\n").encode()
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
                # Stop pumping once the client goes away — the router
                # proxy closes the upstream body on disconnect, and this
                # keeps a backgrounded/closed tab from parking a
                # subscriber forever.
                if await request.is_disconnected():
                    break
        finally:
            operator_events.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Defeat proxy response buffering so frames flush live.
            "X-Accel-Buffering": "no",
        },
    )
