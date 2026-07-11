"""ToolResult — the typed return shape of a tool implementation.

Wave 6 PR 0 of 7 (retire-system-token follow-up). See the Wave 6
section of ``/home/dennis/.claude/plans/prancy-napping-pie.md``.

Why this exists
---------------
Tool impls today return ``list[mcp_types.TextContent]`` for both
success and failure. The REST adapter at ``app/routes.py`` (lines
170-200) text-matches the message body with a regex to decide
whether the tool succeeded, returned a not-found, or refused. The
regex has to stay in sync with each tool's exact wording — every
new wording variant is a silent 200 the dashboard renders as
success.

``ToolResult`` is a sum type so the wire-shape decision is made
once, by the tool, in a typed way. Both consumers (the REST
adapter and the MCP wire renderer) ``match`` on the variant — no
regex.

The bridge in ``tools/registry.dispatch_tool_call`` auto-wraps
old-style ``list[TextContent]`` returns into ``Ok(message=...)``
so this PR doesn't break any unmigrated tool. PR 6 deletes the
bridge after PRs 1-5 migrate every tool.

Variant semantics
-----------------
* :class:`Ok` — the operation succeeded. ``data`` carries
  whatever payload the caller wants (typically a dict; the REST
  adapter JSON-serializes it). ``message`` is an optional
  human-readable summary. When both are set the MCP wire renderer
  emits TWO ``TextContent`` blocks (message first as the
  human-readable summary; data second as its JSON serialisation)
  so MCP clients see both the prose AND the actionable payload —
  the prior "message wins, data dropped" behaviour silently lost
  the token + snippet that ``register_agent`` returns over the
  MCP wire.
* :class:`NotFound` — the named resource doesn't exist. The
  REST adapter maps this to 404.
* :class:`PermissionDenied` — the caller's principal doesn't
  satisfy the tool's policy. REST → 403. This is for per-tool
  policy violations (e.g. "only the note's author can delete it")
  — the outer auth seam (cookie / bearer) already gates "did the
  caller authenticate at all".
* :class:`Invalid` — caller supplied bad input (missing field,
  wrong type, out-of-range value). REST → 400. ``field`` is the
  offending input name when one can be named.
* :class:`Conflict` — the operation would violate a uniqueness
  or state invariant (duplicate agent_id, status transition not
  allowed). REST → 409.
* :class:`Failed` — internal error that isn't the caller's
  fault. REST → 500. Reserve for "the DB write didn't return a
  row id" — exceptions still propagate normally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, List, Optional, Union

import mcp.types as mcp_types

from .config import logger


@dataclass(frozen=True)
class Ok:
    """The tool succeeded."""

    data: Any = None
    message: Optional[str] = None


@dataclass(frozen=True)
class NotFound:
    """A named resource doesn't exist."""

    resource: str
    identifier: str


@dataclass(frozen=True)
class PermissionDenied:
    """The caller's principal didn't satisfy the tool's policy.

    Distinct from a missing authentication — the auth seam already
    rejected unauthenticated calls upstream of the dispatcher.
    """

    reason: str


@dataclass(frozen=True)
class Invalid:
    """Caller-supplied input failed validation.

    ``field`` is the offending input name when one can be named;
    None for cross-field validation errors that don't single out
    one input.
    """

    message: str
    field: Optional[str] = None


@dataclass(frozen=True)
class Conflict:
    """Operation would violate a uniqueness or state invariant."""

    reason: str


@dataclass(frozen=True)
class Failed:
    """Internal failure that isn't the caller's fault."""

    message: str


ToolResult = Union[Ok, NotFound, PermissionDenied, Invalid, Conflict, Failed]


# ── MCP wire renderer ─────────────────────────────────────────────


def render_as_text_content(result: ToolResult) -> List[mcp_types.TextContent]:
    """Convert a :data:`ToolResult` into the
    ``list[mcp_types.TextContent]`` shape MCP tool callers see today.

    Used by the bridge in ``tools/registry.dispatch_tool_call`` so a
    new-style (returns :data:`ToolResult`) tool keeps the same wire
    shape on the MCP path the SSE/JSON-RPC clients consume — same
    code path, same look on the wire. The REST adapter consumes the
    ``ToolResult`` directly and synthesizes a JSON response with a
    matching HTTP status code; this renderer is purely for the MCP
    wire which only speaks text-content blocks.

    Success rendering:
        * When BOTH ``message`` and ``data`` are set, emit TWO
          ``TextContent`` blocks — the message first (prose
          summary for the human/LLM reader) and the JSON-serialised
          data second (the actionable payload). MCP rev 2025-03-26
          allows a tools/call response to carry multiple content
          blocks (``content: [TextContent, ...]``); MCP clients
          treat the array as the full response and render both.
        * When only ``message`` is set, emit one block with the
          message.
        * When only ``data`` is set, emit one block with the JSON
          serialisation (string ``data`` is passed through
          verbatim, never double-encoded).
        * When neither is set, emit one empty-text block (the
          legacy "no output" shape, preserved so tools that
          deliberately return ``Ok()`` keep the same wire shape).

    The pre-fix behaviour treated ``message`` as winner-takes-all
    and silently dropped ``data`` whenever a tool set both. This
    is why ``register_agent`` over the MCP wire surfaced the prose
    summary but not the agent token + ``.mcp.json`` snippet that
    operators need to wire the agent into a claude session. The
    REST adapter already emits both correctly — only the MCP wire
    renderer lost the data field.

    Error rendering:
        Error variants render to a single human-readable
        ``TextContent`` block (``"Unauthorized: ..."`` for
        :class:`PermissionDenied`, ``"Error: ..."`` for the rest).
        This renderer decides the *text* only — it does NOT set the
        MCP ``isError`` flag. The framework keys ``isError`` off
        whether the ``call_tool`` handler RAISES (raised → the
        framework's ``_make_error_result`` sets ``isError=True``); the
        ``"Error:"`` text prefix has no bearing on it. The MCP wire
        handler (``app.main_app.mcp_call_tool_handler``) therefore
        consults :func:`is_error_result` on the variant and builds a
        ``CallToolResult`` with ``isError`` set explicitly, so a
        RETURNED denial reaches the client with ``isError=True`` just
        like a RAISED one. See finding AS-1 (round 3).
    """
    if isinstance(result, Ok):
        # Render ``data`` as a string for the second-block path
        # (or the single-block "data-only" path). String ``data``
        # passes through verbatim — JSON-encoding a string would
        # wrap it in extra quotes and waste a parse on the client.
        def _data_to_text(d: Any) -> str:
            if isinstance(d, str):
                return d
            try:
                return json.dumps(d, default=str)
            except (TypeError, ValueError):
                # Defensive — exotic types fall back to str(); never
                # crash the renderer on a tool's well-typed return.
                return str(d)

        if result.message is not None and result.data is not None:
            # Both set — emit message + data as two blocks so the
            # MCP client sees the actionable payload alongside the
            # prose summary. See module docstring for the
            # register_agent bug this fixes.
            return [
                mcp_types.TextContent(type="text", text=result.message),
                mcp_types.TextContent(
                    type="text", text=_data_to_text(result.data),
                ),
            ]
        if result.message is not None:
            text = result.message
        elif result.data is not None:
            text = _data_to_text(result.data)
        else:
            text = ""
        return [mcp_types.TextContent(type="text", text=text)]

    if isinstance(result, NotFound):
        text = f"Error: {result.resource} {result.identifier!r} not found."
    elif isinstance(result, PermissionDenied):
        text = f"Unauthorized: {result.reason}"
    elif isinstance(result, Invalid):
        if result.field:
            text = f"Error: invalid {result.field}: {result.message}"
        else:
            text = f"Error: invalid input: {result.message}"
    elif isinstance(result, Conflict):
        text = f"Error: conflict: {result.reason}"
    elif isinstance(result, Failed):
        # SEC-R8-1: ~40 tool impls return ``Failed(message=f"…{e}")`` built
        # from a caught sqlite3/SQLAlchemy error, so ``result.message`` can
        # embed table/column names, filesystem paths, and internals. The
        # RAISED-exception paths were genericized in round 7 (SD-R7-1); this
        # is the RETURNED half. Treat ``Failed.message`` as INTERNAL: log it
        # server-side, render a STATIC generic string to the client.
        # ``isError`` fidelity is preserved — ``is_error_result`` still flags
        # ``Failed`` so the MCP handler sets ``isError=True`` (finding AS-1).
        logger.error("Tool returned Failed result: %s", result.message)
        text = "Error: Operation failed"
    else:  # pragma: no cover - defensive
        text = f"Error: unknown ToolResult variant: {result!r}"

    return [mcp_types.TextContent(type="text", text=text)]


# ── HTTP renderer ─────────────────────────────────────────────────


# Variant → HTTP status. ONE authority, shared by every REST consumer
# (``app/_dispatch_helpers._dispatch_through_tool`` and the per-resource
# routers under ``app/routers/``).
#
# TIEBREAK (locked, arch-deepening candidate C): ``PermissionDenied →
# 403`` — the caller authenticated but lacks the *capability* the tool
# requires (authenticated-but-forbidden). ``401`` is deliberately NOT
# used here: it stays reserved for missing / invalid credentials, which
# the auth middleware returns UPSTREAM of dispatch before any
# ``ToolResult`` exists. The register-agent route historically returned
# 401 for ``PermissionDenied`` — that was a divergence from the shared
# dispatcher's 403; collapsing both onto this table fixes it.
_STATUS_BY_VARIANT = {
    Ok: 200,
    NotFound: 404,
    PermissionDenied: 403,
    Invalid: 400,
    Conflict: 409,
    Failed: 500,
}


def tool_result_to_http(result: ToolResult) -> tuple[int, dict[str, Any]]:
    """Convert a :data:`ToolResult` into an ``(http_status, json_body)``
    pair — the single ToolResult→HTTP adapter every REST consumer shares.

    Returns the canonical dashboard JSON body (the
    ``{"success": bool, ...}`` shape the dashboard's ApiClient already
    consumes) alongside the status from :data:`_STATUS_BY_VARIANT`.
    Consumers that need a different body shape (e.g. the register-agent
    route flattens ``Ok.data`` into named fields, and replies to errors
    with a thin ``{"message": ...}``) take the STATUS from here and keep
    their own body — the status mapping is what's unified, so the
    403-vs-401 divergence can't reappear.

    ``Failed`` renders a STATIC ``"Operation failed"`` message: ~40 tool
    impls build ``Failed(message=f"…{e}")`` from a caught DB error, so
    the raw text can embed schema / paths / internals (SEC-R8-1). The
    caller logs the real ``result.message`` server-side; the client sees
    the generic string. This mirrors :func:`render_as_text_content`'s
    Failed handling on the MCP wire.
    """
    status = _STATUS_BY_VARIANT.get(type(result), 500)

    if isinstance(result, Ok):
        body: dict[str, Any] = {
            "success": True,
            "message": result.message or "",
        }
        if result.data is not None:
            body["data"] = result.data
        return status, body

    if isinstance(result, NotFound):
        text = f"{result.resource} {result.identifier!r} not found."
        return status, {
            "success": False,
            "error": "not_found",
            "resource": result.resource,
            "identifier": result.identifier,
            "message": text,
        }

    if isinstance(result, PermissionDenied):
        return status, {
            "success": False,
            "error": "permission_denied",
            "reason": result.reason,
            "message": result.reason,
        }

    if isinstance(result, Invalid):
        return status, {
            "success": False,
            "error": "invalid",
            "field": result.field,
            "message": result.message,
        }

    if isinstance(result, Conflict):
        return status, {
            "success": False,
            "error": "conflict",
            "reason": result.reason,
            "message": result.reason,
        }

    if isinstance(result, Failed):
        # Generic message only — see docstring (SEC-R8-1). The caller
        # logs ``result.message`` server-side; it never reaches the wire.
        return status, {
            "success": False,
            "error": "failed",
            "message": "Operation failed",
        }

    # Defensive — an unknown variant maps to a generic 500.
    return 500, {  # pragma: no cover - defensive
        "success": False,
        "error": "failed",
        "message": "Operation failed",
    }


def is_error_result(result: ToolResult) -> bool:
    """Whether a :data:`ToolResult` represents a failure.

    :class:`Ok` is the sole success variant; every other variant
    (NotFound / PermissionDenied / Invalid / Conflict / Failed) is an
    error. The MCP wire handler uses this to set ``CallToolResult.isError``
    so a RETURNED denial reaches the client with the same ``isError=True``
    the framework sets for a RAISED exception — one authority, both
    paths agree (finding AS-1, round 3).
    """
    return not isinstance(result, Ok)


__all__ = [
    "Ok",
    "NotFound",
    "PermissionDenied",
    "Invalid",
    "Conflict",
    "Failed",
    "ToolResult",
    "render_as_text_content",
    "tool_result_to_http",
    "is_error_result",
]
