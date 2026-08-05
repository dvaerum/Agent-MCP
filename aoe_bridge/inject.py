"""Mode-aware injection of nudge text into an AoE session (ADR-0021).

An AoE session is either a **terminal** (tmux) session or a **structured**
(ACP/CityHall) session, and they take injected text through different
non-owner-scoped REST routes:

- terminal  → ``POST /api/sessions/<id>/send``       ``{message, revive}``
  (keystroke injection; ``revive`` wakes a dormant pane; does not abort a
  running turn)
- structured → ``POST /api/sessions/<id>/acp/prompt`` ``{prompt}``
  (a real ACP prompt turn; wakes an idle-dormant/sunk session first)

Both reach an arbitrary session the plugin didn't create (unlike the
creator-scoped ``sessions.turn.send`` host RPC), which is why the bridge
uses AoE's own localhost REST with the serve token.

:func:`injection_request` is the pure request builder (unit-tested);
:class:`AoeInjector` performs it with an injected async POST callable so the
HTTP client is swappable in tests.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Tuple

# Normalised session modes.
TERMINAL = "terminal"
STRUCTURED = "structured"


def normalise_mode(raw: str | None) -> str:
    """Map AoE's per-session ``tool``/mode hints to TERMINAL/STRUCTURED.

    Anything ACP/structured/composer/cityhall → STRUCTURED; everything else
    (tmux/terminal/shell, or unknown) → TERMINAL, the broadest-reaching
    route."""
    m = (raw or "").strip().lower()
    if any(k in m for k in ("structured", "acp", "composer", "cityhall")):
        return STRUCTURED
    return TERMINAL


def injection_request(
    session_id: str, mode: str | None, text: str
) -> Tuple[str, Dict[str, Any]]:
    """Return ``(path, json_body)`` for injecting ``text`` into ``session_id``.

    ``mode`` is normalised first. Terminal uses ``/send`` with ``revive`` to
    wake a dormant pane; structured uses ``/acp/prompt``."""
    if normalise_mode(mode) == STRUCTURED:
        return (f"/api/sessions/{session_id}/acp/prompt", {"prompt": text})
    return (
        f"/api/sessions/{session_id}/send",
        {"message": text, "revive": True},
    )


class AoeInjector:
    """Inject text into AoE sessions over AoE's localhost REST.

    ``post`` is an ``async (path, json) -> status_code`` callable (a thin
    wrapper over the plugin's HTTP client, carrying the serve token +
    base URL) — injected so tests don't need a live AoE."""

    def __init__(
        self,
        post: Callable[[str, Dict[str, Any]], Awaitable[int]],
    ) -> None:
        self._post = post

    async def inject(self, session_id: str, mode: str | None, text: str) -> bool:
        """Inject ``text``; return True on a 2xx from AoE."""
        path, body = injection_request(session_id, mode, text)
        status = await self._post(path, body)
        return 200 <= status < 300
