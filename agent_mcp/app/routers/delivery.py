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
from ...core.stream_gates import RevalidatingStream, StreamRevoked
from ...features import delivery_transport
from ...repositories.agent_repository import is_active_agent
from ...utils.json_utils import get_sanitized_json_body


router = APIRouter(prefix="/api", tags=["delivery"])

# sse-starlette ping keepalive + dead-peer detector + generator-cancel on
# disconnect (see events.py). Same value as the operator stream.
PING_SECONDS = 15

# How often an OPEN delivery stream re-checks that its bearer is still
# live (R13-F2) — a stream opened BEFORE revocation must be torn down
# rather than survive it (AC-R29-1 class). Kept ≤ PING_SECONDS so
# revocation latency never exceeds one keepalive interval.
#
# N5: the CADENCE half of what this stream hands the shared
# ``core.stream_gates.RevalidatingStream`` seam; ``is_active_agent`` (the
# canonical LIVE_AGENT_SQL repository predicate — deliberately NOT the
# GET /mcp pump's cheaper in-memory cache read, see the seam's module
# docstring) is the other half.
REVALIDATE_SECONDS = PING_SECONDS


def require_agent_bearer(authorization: str = Header(None)) -> str:
    """Resolve the worker's ``agent_id`` from its ``Authorization: Bearer``
    token, or 401. This is the AGENT identity gate (the delivery channel is
    keyed per worker), NOT the operator-session gate the dashboard uses.

    R13-F2: existence is not liveness. ``get_agent_id`` →
    ``get_by_token`` resolves the row for ANY status (its docstring says
    "NOT an auth gate"), so a terminated / tombstone bearer still
    resolved and passed here even though it 401s on ``/mcp``. We gate on
    the canonical DB-backed liveness predicate (``LIVE_AGENT_SQL`` —
    excludes ``terminated`` AND ``tombstone``) so a revoked bearer 401s,
    closing the SEC-A/B / AC-R29-1 liveness-vs-existence class on this
    path too."""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    # A reserved ``__tombstone_*`` token is an FK artefact, never a real
    # bearer — reject before identity resolution (belt-and-braces: the
    # liveness check below also excludes tombstone rows).
    if token and token.startswith("__tombstone_"):
        raise HTTPException(status_code=401, detail="agent bearer required")
    agent_id = get_agent_id(token) if token else None
    if not agent_id or not is_active_agent(agent_id):
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
        # R13-F2, via the N5 seam: a stream authenticates its bearer ONCE
        # at open then pumps indefinitely, so the wait for the next frame
        # and the liveness re-check are ONE call — no frame is dequeued
        # without ``is_active_agent`` having just run, and none is wired
        # to a bearer that went away while it sat in the FIFO.
        # EventSourceResponse still cancels this on client disconnect,
        # unblocking into the finally below.
        gate = RevalidatingStream(
            sub.queue,
            liveness=lambda: is_active_agent(agent_id),
            # Re-read per slice, not captured: the R13-F2 regression test
            # monkeypatches this module attribute.
            interval=lambda: REVALIDATE_SECONDS,
        )
        try:
            while True:
                try:
                    sl = await gate.next_slice()
                except StreamRevoked:
                    return
                if sl.idle:
                    continue
                yield {"data": json.dumps(sl.item)}
        finally:
            delivery_transport.unsubscribe(sub)

    return EventSourceResponse(frame_gen(), ping=PING_SECONDS)


@router.post("/delivery/status")
async def delivery_status(
    request: Request,
    agent_id: str = Depends(require_agent_bearer),
) -> JSONResponse:
    """Record the worker's runtime-reported ``transport-status``."""
    # R13-F3: route the body through the canonical object-guarding
    # sanitizer (like every other app/routers/ body route) rather than a
    # raw ``request.json()``. A truthy non-dict JSON value ([1,2,3], 42,
    # "idle", true) used to survive ``(body or {}).get`` → AttributeError
    # → unhandled 500; the helper raises ValueError for a non-object body,
    # which we map to a clean 400.
    try:
        body = await get_sanitized_json_body(request)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="request body must be a JSON object"
        )
    status = body.get("status")
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
