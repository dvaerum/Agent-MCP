"""Event-loop profile-review surface (agent self-service profiles, plan §7 PR3).

The agent's OWN profile is surfaced in a ``profile_review`` section on the
event-loop tools (``wait_for_events`` / ``fetch_events_since``), fired when
EITHER:

* it is the **first event-loop call of this connection** (greet-once —
  delivers a manager its charter, prompts a blank worker to author one), OR
* the profile is **overdue** for review (last-reviewed age exceeds the
  configured cadence).

Nothing rides ``get_system_prompt`` (locked decision 7): zero standing
per-turn token cost — the profile appears exactly when review is being
asked for. Conceptually a sibling of the shipped unread-message nudge
(``core/unread_nudge.py``), but carried on the loop, not ambiently on many
tools. DB-stateless: the greet flag lives in ``core/session_registry`` (in
memory), keyed to the GET /mcp connection lifecycle.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

from .config import logger
from .principal import Principal


def _parse_iso(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def build_profile_review_section(
    principal: Optional[Principal],
) -> Optional[Dict[str, Any]]:
    """Return the ``profile_review`` section for this event-loop call, or
    ``None`` when nothing needs surfacing.

    Fires (and marks the connection greeted) when it is the first
    event-loop call of the connection OR the profile is overdue. Gated on
    ``principal.kind == "agent_bearer"`` — operators / anonymous never see
    it. ``config_profile_review_interval_days`` (default 7) sets the
    overdue window; ``0`` disables the overdue trigger but NOT the
    first-connect greet.

    The section carries the caller's CURRENT profile (so it verifies
    against real content), its last-reviewed age, an instruction to
    confirm/refresh via ``update_agent_profile``, and a one-line pointer to
    ``view_agents``.
    """
    if principal is None or principal.kind != "agent_bearer":
        return None
    agent_id = principal.agent_id
    if not agent_id:
        return None

    try:
        from . import session_registry
        from ..repositories import agent_repo
        from ..tools import access as _access

        row = agent_repo.get_by_id(agent_id)
        if row is None:
            return None

        interval_days = _access._get_config_int(
            "config_profile_review_interval_days"
        )

        reviewed_at_raw = row.get("profile_reviewed_at")
        reviewed_dt = _parse_iso(reviewed_at_raw)
        now = datetime.datetime.now()

        age_days: Optional[float] = None
        if reviewed_dt is not None:
            age_days = (now - reviewed_dt).total_seconds() / 86400.0

        # Overdue when never reviewed, or older than the window. A
        # window of 0 disables the overdue trigger (but not the greet).
        overdue = False
        if interval_days and interval_days > 0:
            overdue = reviewed_dt is None or (
                age_days is not None and age_days > interval_days
            )

        first = not session_registry.is_profile_greeted(agent_id)
        if not (first or overdue):
            return None

        # Mark greeted so the same connection's subsequent loop calls omit
        # the greet (the overdue path still re-surfaces until reviewed).
        session_registry.mark_profile_greeted(agent_id)

        profile = row.get("profile")
        has_profile = bool(profile and profile.strip())
        if not has_profile:
            instruction = (
                "You have no profile yet. Author one with "
                "`update_agent_profile(profile=\"…\")`: a short description "
                "of what you do, what you work on, what tools you have, how "
                "you work, and what peers should ask you about."
            )
        else:
            instruction = (
                "Verify this is still accurate. Call "
                "`update_agent_profile(profile=\"…\")` to refresh it, or "
                "`update_agent_profile()` with no argument to confirm it is "
                "still correct (records a review without changing anything)."
            )

        section: Dict[str, Any] = {
            "reason": "first_connect" if first and not overdue else (
                "overdue" if overdue and not first else "first_connect_overdue"
            ),
            "profile": profile,
            "profile_reviewed_at": reviewed_at_raw,
            "last_reviewed_age_days": (
                round(age_days, 2) if age_days is not None else None
            ),
            "instruction": instruction,
            "roster_hint": (
                "Call `view_agents` to see the whole team's profiles "
                "(who to ask for what)."
            ),
        }
        return section
    except Exception:  # noqa: BLE001 — a review section must never break the loop
        logger.debug(
            "profile-review section build failed for %r; omitting",
            agent_id,
            exc_info=True,
        )
        return None


__all__ = ["build_profile_review_section"]
