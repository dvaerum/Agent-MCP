"""Default agent profile templates (agent self-service profiles, plan §2.6).

Workers start with an EMPTY profile and build it up as they go — there
is no worker default. Managers get a hardcoded charter seeded at
registration (``register_agent_tool_impl``), editable afterward like any
other profile. Seeding stamps ``profile_reviewed_at = profile_updated_at
= created_at`` so a fresh manager is not instantly "stale".

Keeping the template here (one module-level constant) rather than inline
in the register flow means the seed text has one canonical home the
tests pin and the review-nudge copy can reference.
"""

from __future__ import annotations


#: Seeded onto every ``manager``-role agent at registration. The charter
#: tells a manager what its role entails; it reads its own copy on the
#: first event-loop call (the ``profile_review`` greet, PR3) and can
#: refine it via ``update_agent_profile`` thereafter.
MANAGER_DEFAULT_PROFILE: str = (
    "You are a manager. Your role:\n"
    "- Break down and assign work to the workers on your team, and review "
    "what they deliver.\n"
    "- Curate your team's profiles: keep each worker's `profile` accurate "
    "(who does what, what tools they have, what to ask them about) so the "
    "team can find the right person.\n"
    "- Coordinate across the team — route questions, unblock workers, and "
    "keep shared context current.\n"
    "\n"
    "Replace this charter with a description of how YOU actually operate: "
    "your focus areas, the parts of the system you own, and what peers "
    "should come to you for. Call `update_agent_profile` to update it, or "
    "to confirm it is still accurate."
)


__all__ = ["MANAGER_DEFAULT_PROFILE"]
