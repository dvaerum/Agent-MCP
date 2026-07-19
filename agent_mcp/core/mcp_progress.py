"""Read/write MCP progress state for the in-flight tool call.

The event-loop long-hold feature keeps ``wait_for_events`` open for a
heartbeat-capable client by emitting ``notifications/progress`` frames on
the request's own MCP session — the client resets its idle timeout on
each frame (see :mod:`agent_mcp.core.client_hold_strategy`).

Both helpers read the MCP Python SDK's request-scoped
``request_ctx`` ContextVar (``mcp.server.lowlevel.server.request_ctx``),
which the SDK sets per tool-call before dispatch. When there is no live
MCP request context — the REST adapter, the pytest harness that calls the
tool handler directly, any in-process caller — the ContextVar is unset and
both helpers degrade to "no progress token / no-op send" so the wait loop
transparently falls back to a silent hold.
"""

from __future__ import annotations

from typing import Optional, Union

from .config import logger

ProgressToken = Union[str, int]


def _request_ctx():
    """Return the SDK's current RequestContext, or ``None`` off-wire."""
    try:
        from mcp.server.lowlevel.server import request_ctx
    except Exception:  # pragma: no cover - SDK always importable in prod
        return None
    try:
        return request_ctx.get()
    except LookupError:
        # No MCP request in flight (REST / harness / in-process caller).
        return None


def current_progress_token() -> Optional[ProgressToken]:
    """The ``_meta.progressToken`` the client attached to this tool call.

    Returns ``None`` when the caller sent no token OR there is no live MCP
    request context. A non-None value is the feature-detect signal that an
    unknown client MAY be heartbeat-capable (identity table still wins for
    known clients — see ``resolve_hold_strategy``).
    """
    ctx = _request_ctx()
    if ctx is None:
        return None
    meta = getattr(ctx, "meta", None)
    if meta is None:
        return None
    return getattr(meta, "progressToken", None)


async def send_progress_heartbeat(
    progress_token: ProgressToken, progress: float
) -> bool:
    """Emit one ``notifications/progress`` frame on the in-flight session.

    ``progress`` should increase monotonically across a single request
    (per the MCP spec). ``related_request_id`` is threaded so the client
    associates the frame with THIS tool call. Returns ``True`` when a
    frame was sent, ``False`` off-wire or on any send failure (a failed
    heartbeat must never crash the wait loop — the connection simply
    behaves as a silent hold for that tick).
    """
    ctx = _request_ctx()
    if ctx is None:
        return False
    session = getattr(ctx, "session", None)
    if session is None:
        return False
    try:
        await session.send_progress_notification(
            progress_token=progress_token,
            progress=progress,
            total=None,
            related_request_id=getattr(ctx, "request_id", None),
        )
        return True
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("wait_for_events progress heartbeat send failed: %s", e)
        return False
