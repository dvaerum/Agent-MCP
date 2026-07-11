"""Coupling guard: ``tools/list`` visibility must track the LIVE
``@requires_capability`` gate, not a hand-synced ``visibility=`` kwarg.

Before arch-r3 #1+5 PR-A, ``tools/access._derive_access_level`` read the
DEAD ``impl._required_role`` attribute (set nowhere post-Wave-9) and
NEVER read the live ``impl._required_capability`` (set by
``@requires_capability`` at ``core/authorize.py``). Visibility silently
fell back to the ``visibility=`` kwarg, so a capability-gated tool could
appear in a caller's ``tools/list`` even though its cap gate would reject
that caller — a leak nothing tested.

These tests couple the two: the derived visibility of every cap-gated
tool must never be MORE permissive than the capability bundle for that
cap. They are RED against the pre-PR-A derivation (which ignored the
capability entirely) and GREEN after it reads ``_required_capability``.
"""
from __future__ import annotations

import agent_mcp.tools  # noqa: F401 — import for side effect: registers tools
from agent_mcp.core.capabilities import AGENT_ROLE_BUNDLES
from agent_mcp.tools.access import TOOL_ACCESS, is_visible_to_role
from agent_mcp.tools.registry import tool_registry


# Restrictiveness rank of a derived visibility level. Lower = fewer roles
# admitted. A cap-gated tool's derived level may be EQUAL to or MORE
# restrictive than its cap tier (an explicit tighten override), never
# LESS (that is the leak the coupling forbids).
_RANK = {"operator": 0, "admin": 0, "manager": 1, "worker": 2, "any": 3}


def _cap_tier(cap: str) -> str:
    """The visibility tier implied by a capability, via the bundles."""
    if cap in AGENT_ROLE_BUNDLES["worker"]:
        return "worker"
    if cap in AGENT_ROLE_BUNDLES["manager"]:
        return "manager"
    return "operator"


def _cap_gated_tools():
    for name in tool_registry.names():
        entry = tool_registry.get(name)
        if entry is None:  # pragma: no cover — names()/get() agree
            continue
        cap = getattr(entry.meta.implementation, "_required_capability", None)
        if cap is not None:
            yield name, cap, entry


def test_there_are_cap_gated_tools() -> None:
    """Guard the guard: if registration ever stops setting
    ``_required_capability`` the coupling tests would pass vacuously.
    """
    assert list(_cap_gated_tools()), (
        "no tool exposes _required_capability — the coupling tests would "
        "pass vacuously; check @requires_capability registration"
    )


def test_derived_visibility_never_looser_than_capability_tier() -> None:
    """Every cap-gated tool's derived visibility is EQUAL to, or more
    restrictive than, the tier its capability bundle implies.

    RED against pre-PR-A code: e.g. ``view_tasks`` / ``create_self_task``
    (cap in the worker bundle) derived ``"any"`` (rank 3) which is looser
    than the ``"worker"`` tier (rank 2) — a leak to anonymous callers.
    """
    offenders = []
    for name, cap, _entry in _cap_gated_tools():
        derived = TOOL_ACCESS.get(name)
        cap_tier = _cap_tier(cap)
        # worker-if-toggled is worker-conditional; treat as worker rank.
        derived_rank = _RANK.get(
            "worker" if str(derived).startswith("worker-if-toggled:") else derived
        )
        if derived_rank is None or derived_rank > _RANK[cap_tier]:
            offenders.append((name, cap, derived, cap_tier))
    assert not offenders, (
        "cap-gated tools whose derived visibility is LOOSER than their "
        f"capability tier (leak): {offenders}"
    )


def test_cap_gated_tool_never_visible_to_a_role_the_cap_rejects() -> None:
    """The behavioural form: a cap-gated tool is never surfaced to a role
    that its capability bundle does not grant.

    Anonymous callers hold no caps → must never see a cap-gated tool.
    Worker callers see a cap-gated tool only if the worker bundle grants
    the cap. RED against pre-PR-A code (anonymous saw ``view_tasks`` etc.).
    """
    worker_caps = AGENT_ROLE_BUNDLES["worker"]
    anon_leaks, worker_leaks = [], []
    for name, cap, _entry in _cap_gated_tools():
        if is_visible_to_role(name, "anonymous"):
            anon_leaks.append((name, cap))
        if is_visible_to_role(name, "worker") and cap not in worker_caps:
            worker_leaks.append((name, cap))
    assert not anon_leaks, f"anonymous can see cap-gated tools: {anon_leaks}"
    assert not worker_leaks, (
        f"worker can see cap-gated tools the worker bundle rejects: "
        f"{worker_leaks}"
    )


def test_cap_gated_tool_without_explicit_kwarg_is_not_any() -> None:
    """A capability-gated tool that ships with the DEFAULT
    ``visibility="any"`` (no explicit override) must not derive to
    ``"any"`` — the derivation reads its capability instead.

    RED against pre-PR-A code (which ignored the cap and returned the
    ``"any"`` default verbatim).
    """
    offenders = []
    for name, cap, entry in _cap_gated_tools():
        if entry.meta.declared_visibility == "any":  # default / no override
            if TOOL_ACCESS.get(name) == "any":
                offenders.append((name, cap))
    assert not offenders, (
        "cap-gated tools defaulting to visibility='any' still derive "
        f"'any' (cap ignored): {offenders}"
    )
