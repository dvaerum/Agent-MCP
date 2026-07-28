"""Operator dashboard live-update SSE channel + observability.

``GET /api/events`` — a long-lived ``text/event-stream`` the dashboard's
notification client opens; it refetches its pages whenever the backend
signals that project data changed. Every mutation logs an action at the
``log_agent_action_to_db`` choke point, which publishes a
``notifications/resources/updated`` envelope onto the in-process
``features.operator_events`` hub; this endpoint drains that hub onto the
wire.

``GET /api/events/status`` — operator observability: how many dashboard
streams are live, who owns them, and each one's age + queue depth
(``snapshot()``). Lets an operator SEE liveness / spot a leak instead of
trusting it.

Transport: built on :class:`sse_starlette.sse.EventSourceResponse`
rather than a hand-rolled generator. sse-starlette owns the two things
easy to get subtly wrong — a periodic ping keepalive (which also
surfaces a dead peer when the write fails) and cancelling the content
generator on client disconnect (so the ``finally`` reliably
unsubscribes). We just subscribe, drain, and unsubscribe.

Why a dedicated endpoint (not the ``/mcp`` transport): the MCP
StreamableHTTP GET stream needs an ``Mcp-Session-Id`` from a prior
per-agent ``initialize`` the cookie-only dashboard never performs (405),
and operators are not agents so the agent-scoped ``session_registry``
(``agent_id`` FK) can't hold them. This endpoint authenticates the
operator session cookie via ``require_operator_session``.

Final URLs: backend ``/api/events`` + ``/api/events/status`` → dashboard
``/agent-mcp/api/<project>/events`` (+ ``/status``). The router's
``/api/...`` proxy streams the response body chunk-by-chunk, so SSE
frames flow through untouched (and the router exempts the ``events``
stream from the JSON Accept-version gate).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ..deps import caller_identity, require_operator_session
from ...features import operator_events


router = APIRouter(prefix="/api", tags=["events"])


# sse-starlette sends a ping comment every PING_SECONDS: it keeps the
# connection alive through intermediaries and doubles as the dead-peer
# detector (a ping write to a gone client raises, ending the stream and
# running the finally). It also cancels the generator on client
# disconnect, so a backgrounded/closed tab can't park a subscriber
# forever.
PING_SECONDS = 15


@router.get("/events")
async def operator_events_stream(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> EventSourceResponse:
    """Stream ``notifications/resources/updated`` envelopes to an
    authenticated operator as Server-Sent Events."""
    user_id = caller_identity(auth)

    async def event_gen():
        sub = operator_events.subscribe(user_id=user_id)
        try:
            while True:
                # EventSourceResponse cancels this coroutine on client
                # disconnect, so a blocked get() unblocks into the
                # finally below; pings are emitted by sse-starlette
                # independently of this loop.
                item = await sub.queue.get()
                yield {"data": json.dumps(item)}
        finally:
            operator_events.unsubscribe(sub)

    return EventSourceResponse(event_gen(), ping=PING_SECONDS)


@router.get("/events/status")
async def operator_events_status(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Operator observability for the live-update channel: the count of
    live dashboard SSE streams plus a per-stream snapshot
    (``user_id`` / ``connected_at`` / ``age_seconds`` / ``queue_depth``).
    A JSON REST endpoint (subject to the normal Accept-version gate),
    unlike the ``events`` stream itself."""
    return JSONResponse(
        {
            "connected": operator_events.subscriber_count(),
            "subscribers": operator_events.snapshot(),
        }
    )
