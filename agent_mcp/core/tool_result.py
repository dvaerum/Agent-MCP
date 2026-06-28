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
  human-readable summary; the MCP wire renderer prefers it over
  ``data`` when set, since MCP clients display text-content
  blocks to a human/LLM viewer.
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
        * ``message`` wins when set — that's what the tool author
          chose to surface to a human/LLM.
        * Otherwise ``data`` is JSON-serialized. If ``data`` is
          itself a string, it's passed through verbatim.
        * If neither is set, an empty status block is emitted.

    Error rendering:
        ``"Error: <variant_label>: <detail>"`` — the framework's
        ``isError=true`` handling sees the ``"Error:"`` prefix and
        the dispatcher catches it via the registered tool's exception
        handler. The format mirrors the legacy hand-rolled error
        strings tool impls produced before this PR.
    """
    if isinstance(result, Ok):
        if result.message is not None:
            text = result.message
        elif result.data is None:
            text = ""
        elif isinstance(result.data, str):
            text = result.data
        else:
            try:
                text = json.dumps(result.data, default=str)
            except (TypeError, ValueError):
                # Defensive — exotic types fall back to str(); never
                # crash the renderer on a tool's well-typed return.
                text = str(result.data)
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
        text = f"Error: {result.message}"
    else:  # pragma: no cover - defensive
        text = f"Error: unknown ToolResult variant: {result!r}"

    return [mcp_types.TextContent(type="text", text=text)]


__all__ = [
    "Ok",
    "NotFound",
    "PermissionDenied",
    "Invalid",
    "Conflict",
    "Failed",
    "ToolResult",
    "render_as_text_content",
]
