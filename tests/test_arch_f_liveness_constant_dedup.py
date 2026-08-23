"""Finding F (security-arch-hardening-consolidated.md Phase 1):
``scheduled_directive_tools.TERMINAL_AGENT_STATUSES`` was a verbatim
redeclaration of ``agent_repository.TERMINAL_AGENT_STATUSES`` instead
of an import -- a pure duplicate, no local knowledge, kept in sync only
by two people happening to type the same tuple. This is the trivial
one-liner half of a bigger recurring pattern (see N3/N5 in the
follow-up architecture review: "is this agent still live?" is answered
several different ways across the codebase) -- this fix does only the
scoped duplicate-constant subtraction, not the larger unification.
"""

from __future__ import annotations


def test_terminal_agent_statuses_is_the_same_object_everywhere() -> None:
    """scheduled_directive_tools must import the constant, not
    redeclare it -- ``is`` identity, not just equal values, or a future
    edit to one copy silently drifts from the other."""
    from agent_mcp.repositories.agent_repository import (
        TERMINAL_AGENT_STATUSES as repo_constant,
    )
    from agent_mcp.tools.scheduled_directive_tools import (
        TERMINAL_AGENT_STATUSES as tool_constant,
    )

    assert tool_constant is repo_constant
