# Agent-MCP/agent_mcp/app/instructions_contributors.py
"""Registry for ``serverInfo.instructions`` contributors (PR-W1b).

Background
----------
The MCP `initialize` response carries a top-level ``instructions``
field (per spec rev 2025-03-26, sibling of ``serverInfo`` not nested
inside it) that clients display as authoritative guidance. agent-mcp
appends two blocks to this field today:

  1. **Alias deprecation warning** (ADR-0010 / Phase 1c) — emitted when
     the request arrived via an alias URL with a scheduled expiry.
  2. **Wake-loop bootstrap** (ADR-0011 / event-coord) — emitted when
     the calling bearer's agent has both the global and per-agent
     ``auto_event_loop`` toggles ON.

Pre-PR-W1b both blocks were stitched together inside
``_patched_create_initialization_options`` in ``main_app.py``. Adding
a third contributor meant reaching into that monkeypatch and growing
its conditional chain — a textbook "one reach-in, N implicit
contributors" shape that the 2026-06-05 architecture review flagged
as Finding #3.

This module replaces the inline chain with a small registry:

    InstructionsContributor = Callable[[InitContext], str | None]

    register(name, fn)
    render_all(ctx) -> str

The monkeypatch reduces to a one-liner that calls ``render_all`` and
appends the result. Each contributor:

  * receives an ``InitContext`` carrying the per-request facts it
    needs (bearer + alias info today; extensible by adding fields),
  * returns either the text to append, or ``None`` to opt out for
    this request (its own gating logic).

Ordering matters: alias warnings are about the URL the client is
using *right now* and must precede operational guidance (the wake-
loop) that applies every session. The registry preserves
registration order, which is set explicitly at the bottom of this
module.

Tests in ``tests/test_instructions_contributors.py`` lock the
contract: register/render shape, ordering, None-skipping, and the
end-to-end ``initialize`` integration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional


# --- Per-request context ------------------------------------------


@dataclass(frozen=True)
class InitContext:
    """Per-request facts the contributors gate on.

    Frozen because contributors should never mutate the shared context
    (they only read), and a frozen dataclass is hashable + cheap.

    Fields:
      bearer: the Authorization-header bearer token bound to this
        request via the ``request_auth_token`` ContextVar, or ``None``
        if no bearer (rare — middleware rejects unauthenticated /mcp
        requests with 401, so this is only ``None`` in unit tests).
      alias_info: ``(alias_name, expires_at)`` tuple set by
        ``AuthHeaderMiddleware`` when the upstream router proxied a
        request from an alias URL, or ``None`` for the canonical-URL
        hot path.
      principal: the typed :class:`agent_mcp.core.principal.Principal`
        for the calling request (Wave 6 PR 6). Used by
        :func:`_wake_loop_contributor` to read
        ``principal.can_wake_loop`` directly instead of re-deriving
        the eligibility chain from a bearer.

    Extending: when a new contributor needs a new fact, add a new
    field here with a sensible default (so existing call-sites that
    build an ``InitContext`` don't need to change). The registry
    deliberately doesn't accept ``**kwargs`` — explicit fields keep
    contributor signatures honest.
    """

    bearer: Optional[str]
    alias_info: Optional[tuple[str, str]]
    principal: Optional[object] = None


InstructionsContributor = Callable[[InitContext], Optional[str]]


# --- Registry ------------------------------------------------------


# Module-level ordered list. List (not dict) because the order is
# semantic — contributors render in registration order. The ``name``
# half of each tuple is for log + debug + diagnostics; the registry
# itself does not look up by name.
_contributors: list[tuple[str, InstructionsContributor]] = []


def register(name: str, fn: InstructionsContributor) -> None:
    """Append a contributor to the registry.

    Duplicates by name are allowed (the registry doesn't deduplicate)
    — tests intentionally re-register to swap behaviour, and the
    snapshot/restore pattern used by ``tests/test_instructions_contributors.py``
    handles cleanup. Production code should only call ``register``
    once per contributor at module import time.
    """
    _contributors.append((name, fn))


def render_all(ctx: InitContext) -> str:
    """Concatenate every contributor's output for the given context.

    Contributors returning ``None`` are skipped (no empty-string
    surrogate, no separator). Concatenation matches the pre-refactor
    behaviour exactly: each contributor's text already carries its
    own leading ``\\n\\n`` separator (see ``_ALIAS_WARNING_TEMPLATE``
    and ``WAKE_LOOP_INSTRUCTIONS``), so the registry just joins
    them with ``""``. Returns ``""`` if no contributors contribute,
    so callers can safely do
    ``base.instructions = (base.instructions or "") + render_all(ctx)``
    without an extra conditional.
    """
    parts: list[str] = []
    for _name, fn in _contributors:
        chunk = fn(ctx)
        if chunk is None:
            continue
        parts.append(chunk)
    return "".join(parts)


# --- Built-in contributors ----------------------------------------


def _alias_warning_contributor(ctx: InitContext) -> Optional[str]:
    """ADR-0010 alias deprecation warning.

    Migrated from ``main_app._patched_create_initialization_options``
    pre-PR-W1b. Text content is unchanged — same template, same
    formatting. Gate condition: ``ctx.alias_info`` is set (which the
    middleware does iff the router forwarded an ``X-Agent-MCP-Alias``
    header).
    """
    if ctx.alias_info is None:
        return None
    alias_name, expires_at = ctx.alias_info
    # Imported here (rather than at module top) to avoid an import
    # cycle: main_app imports this module, and the warning builder
    # lives in main_app. Top-of-file import would create a cycle at
    # interpreter startup.
    from .main_app import _build_alias_warning

    return _build_alias_warning(alias_name, expires_at)


def _wake_loop_contributor(ctx: InitContext) -> Optional[str]:
    """ADR-0011 / event-coord wake-loop bootstrap.

    Wave 6 PR 6: the eligibility chain (admin agents skipped, global
    flag, per-agent flag) is resolved once at the middleware seam and
    surfaces here as :attr:`Principal.can_wake_loop`. The contributor
    reads that bit directly — no DB hop, no re-deriving identity.
    """
    if ctx.principal is None:
        return None
    if not getattr(ctx.principal, "can_wake_loop", False):
        return None
    from .event_loop_instructions import WAKE_LOOP_INSTRUCTIONS

    return WAKE_LOOP_INSTRUCTIONS


# --- Built-in registrations ---------------------------------------
# Order matters and is intentional:
#   1. alias-warning — about the URL the client is using right now,
#      most actionable, comes first.
#   2. wake-loop — operational guidance that applies every session.
# Future contributors should slot in based on the same "most-urgent
# first" reasoning.
register("alias-warning", _alias_warning_contributor)
register("wake-loop", _wake_loop_contributor)
