"""Delivery transport — the per-worker fallback push channel (ADR-0021).

Two endpoints, both authenticated by the **worker's own bearer token**
(the same token its MCP tools use — unlike the operator-cookie dashboard
routes):

- ``GET  /api/<project>/delivery/stream`` — SSE **down**. agent-mcp streams
  skinny notification frames the instant the fallback policy fires; the
  runtime (e.g. the AoE bridge) injects them into the session.
- ``POST /api/<project>/delivery/status`` — status **up**. The runtime
  reports this worker's session ``transport-status`` (working / idle /
  dormant / dead), a signal separate from agent-mcp's own presence.

The router's ``/api/<project>/...`` proxy streams the SSE body through, and
the Accept-version gate already exempts any ``text/event-stream`` request,
so no router change is needed for the stream.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ...core.auth import get_agent_id
from ...features import delivery_transport


router = APIRouter(prefix="/api", tags=["delivery"])

# sse-starlette ping keepalive + dead-peer detector + generator-cancel on
# disconnect (see events.py). Same value as the operator stream.
PING_SECONDS = 15


def require_agent_bearer(authorization: str = Header(None)) -> str:
    """Resolve the worker's ``agent_id`` from its ``Authorization: Bearer``
    token, or 401. This is the AGENT identity gate (the delivery channel is
    keyed per worker), NOT the operator-session gate the dashboard uses."""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    agent_id = get_agent_id(token) if token else None
    if not agent_id:
        raise HTTPException(status_code=401, detail="agent bearer required")
    return agent_id


@router.get("/delivery/stream")
async def delivery_stream(
    request: Request,
    agent_id: str = Depends(require_agent_bearer),
) -> EventSourceResponse:
    """Stream skinny delivery frames to the worker's runtime as SSE."""

    async def frame_gen():
        sub = delivery_transport.subscribe(agent_id)
        try:
            while True:
                # EventSourceResponse cancels this on client disconnect, so
                # a blocked get() unblocks into the finally below.
                frame = await sub.queue.get()
                yield {"data": json.dumps(frame)}
        finally:
            delivery_transport.unsubscribe(sub)

    return EventSourceResponse(frame_gen(), ping=PING_SECONDS)


@router.post("/delivery/status")
async def delivery_status(
    request: Request,
    agent_id: str = Depends(require_agent_bearer),
) -> JSONResponse:
    """Record the worker's runtime-reported ``transport-status``."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = (body or {}).get("status")
    if status not in delivery_transport.VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"status must be one of {delivery_transport.VALID_STATUSES}"
            ),
        )
    delivery_transport.set_status(agent_id, status)
    return JSONResponse(
        {"ok": True, "agent_id": agent_id, "status": status}
    )
