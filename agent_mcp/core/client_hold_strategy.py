"""Per-client connection-hold strategy for ``wait_for_events``.

The event-loop long-hold feature (plan: event-loop-longlived-connections)
holds a single ``wait_for_events`` connection open far longer than the
legacy ~60s so an agent burns fewer reconnect model-turns. HOW LONG a
connection may hold — and whether the server emits MCP progress-
notification heartbeats to keep the client from timing out — depends on
the CLIENT, because only some clients reset their idle timeout when they
receive a ``notifications/progress`` frame.

This module owns the hybrid identity-first / feature-detect resolution:

* **Identity table** (:data:`CLIENT_HOLD_STRATEGY`) keyed by the exact
  ``clientInfo.name`` a client sends in its MCP ``initialize`` handshake
  (normalized case/spacing). Researched per-client — see the plan's §3
  behavior table. This is authoritative because pure feature-detection is
  UNSAFE: Cursor sends a ``progressToken`` (to render progress in its UI)
  but never resets its timeout on it, so keying only on "sent a token"
  would hand Cursor a long hold its own timeout then aborts.

* **Feature-detect fallback** for a client NOT in the table: if the
  tool-call carried a ``progressToken`` in its ``_meta`` we assume it is
  heartbeat-capable with no cap; otherwise the safe silent-hold default.

Adding a newly-researched client is a one-line addition to the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# --- Tunables (seconds) ----------------------------------------------------

#: Cadence at which the wait loop emits a ``notifications/progress``
#: heartbeat for a heartbeat-capable client. Must sit comfortably under
#: the tightest heartbeat-resettable idle timeout among heartbeat clients
#: (OpenCode's default is 60s, resets on progress) — 25s leaves ample
#: margin for one dropped/slow frame.
HEARTBEAT_INTERVAL_SECONDS = 25

#: Silent hold for a no-heartbeat client. Sits just under the universal
#: 60s MCP SDK default so the hold returns cleanly (empty envelope →
#: reconnect) BEFORE the client aborts the call. Applies to Cursor / Zed
#: / Cline / Continue and any unknown client that sent no progressToken.
NO_HEARTBEAT_HOLD_SECONDS = 55

#: Claude Code resets its ~5-min idle watchdog on each progress frame, but
#: a SEPARATE ~27.8h wall-clock cap is NOT progress-resettable. Recycle
#: the connection at 24h (comfortably under 27.8h) so the client never
#: hits that hard wall mid-hold.
CLAUDE_CODE_HOLD_CAP_SECONDS = 24 * 60 * 60  # 86400


@dataclass(frozen=True)
class HoldStrategy:
    """How long one ``wait_for_events`` connection may hold, and whether
    to emit heartbeats while it does.

    * ``heartbeat`` — emit ``notifications/progress`` every
      :data:`HEARTBEAT_INTERVAL_SECONDS` to keep the client's idle timer
      from firing. False → silent hold, no progress frames.
    * ``hold_cap`` — max seconds ONE connection may hold before it
      recycles (returns an empty envelope → the agent reconnects).
      ``None`` means "no per-connection recycle" — the connection holds
      until a real event (or, once PR2 lands, the idle-stop window).
    """

    heartbeat: bool
    hold_cap: Optional[int]


# Heartbeat / capped — Claude Code: resets idle watchdog on progress, but
# recycle at 24h to stay under its non-resettable ~27.8h wall-clock cap.
_CLAUDE_CODE = HoldStrategy(heartbeat=True, hold_cap=CLAUDE_CODE_HOLD_CAP_SECONDS)

# Heartbeat / no cap — OpenCode (and unknown-with-progressToken): resets on
# progress with no maxTotalTimeout, so one connection can span the whole
# idle-stop window.
_HEARTBEAT_NO_CAP = HoldStrategy(heartbeat=True, hold_cap=None)

# No heartbeat — a single fixed client timeout == its hard cap. Silent
# ~55s hold, then reconnect.
_NO_HEARTBEAT = HoldStrategy(heartbeat=False, hold_cap=NO_HEARTBEAT_HOLD_SECONDS)


#: Identity table keyed by NORMALIZED ``clientInfo.name`` (see
#: :func:`normalize_client_name`). One row per researched client; add a
#: newly-researched client here in one line. Values from the plan's §3
#: behavior table (all six advertised connect-tab clients).
CLIENT_HOLD_STRATEGY: dict[str, HoldStrategy] = {
    "claude-code": _CLAUDE_CODE,
    "opencode": _HEARTBEAT_NO_CAP,
    "cursor": _NO_HEARTBEAT,   # sends a progressToken but does NOT reset — pin by identity
    "cline": _NO_HEARTBEAT,
    "zed": _NO_HEARTBEAT,
    "continue": _NO_HEARTBEAT,
}


def normalize_client_name(name: Optional[str]) -> Optional[str]:
    """Normalize a raw ``clientInfo.name`` for table lookup.

    Real handshakes vary in case/spacing (``"claude-code"`` vs a
    hypothetical ``"Claude Code"``); we lower-case, strip, and collapse
    internal whitespace to a single space so the table can key on one
    canonical form. Returns ``None`` for an empty/absent name.
    """
    if not name:
        return None
    collapsed = " ".join(str(name).split())
    return collapsed.strip().lower() or None


def resolve_hold_strategy(
    client_name: Optional[str],
    *,
    has_progress_token: bool,
) -> HoldStrategy:
    """Resolve the connection-hold strategy for one ``wait_for_events`` call.

    Hybrid resolution (plan §2 locked decision #1):

    1. Look up the normalized ``client_name`` in
       :data:`CLIENT_HOLD_STRATEGY`. A hit is authoritative — it wins even
       when the call carried a ``progressToken`` (the Cursor false-positive
       guard: Cursor is pinned to no-heartbeat despite sending a token).
    2. Miss → feature-detect: ``has_progress_token`` ⇒ heartbeat / no cap
       (assume a well-behaved unknown client that will reset on progress);
       else the safe silent-hold default.
    """
    normalized = normalize_client_name(client_name)
    if normalized is not None:
        known = CLIENT_HOLD_STRATEGY.get(normalized)
        if known is not None:
            return known
    if has_progress_token:
        return _HEARTBEAT_NO_CAP
    return _NO_HEARTBEAT
