"""arch-deepening candidate B — build the Principal once, at one seam.

Invariants locked here:

* The operator-tier predicate is ONE shared definition. Before this
  candidate, ``core.authorize._is_operator_tier`` and
  ``tools.agent_communication_tools._is_operator_tier`` were two copies
  that had DRIFTED: only the latter honoured the legacy
  ``agent_id == "admin"`` label, so the SAME admin-labelled manager
  Principal was classified operator-tier by one authorization surface
  and non-operator by the other. That drift is the RED this file pins;
  the shared :func:`principal_builder.is_operator_tier` reconciles it.

* Every ``agent_bearer`` construction site resolves capabilities through
  the ONE shared builder, so a synthesized fallback identity can never
  resolve to a different capability set than the middleware seam would
  have produced for the same bearer.

* The operator/forwarding builder resolves capabilities via
  :func:`resolve_capabilities` verbatim — the single path that unions the
  project-role bundle with the group-capability overlay.
"""

from __future__ import annotations

import agent_mcp.core.authorize as authorize
import agent_mcp.core.principal_builder as principal_builder
import agent_mcp.tools.agent_communication_tools as agent_comm
from agent_mcp.core.capabilities import resolve_capabilities
from agent_mcp.core.principal import Principal
from agent_mcp.core.principal_builder import (
    build_agent_bearer_principal,
    build_operator_principal,
    is_operator_tier,
)


def _admin_labelled_manager() -> Principal:
    """The harness's manager-role row labelled ``admin`` — the exact
    identity whose classification the two predicate copies disagreed on.
    """
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id="admin",
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="manager",
        can_wake_loop=False,
        source_token="tok",
    )


# ── Operator-tier predicate: single definition, no drift ────────────


def test_operator_tier_predicate_is_a_single_shared_object() -> None:
    """Both authorization surfaces reference the ONE shared predicate.

    RED before candidate B: ``authorize._is_operator_tier`` and
    ``agent_comm._is_operator_tier`` were distinct function objects.
    """
    assert authorize._is_operator_tier is principal_builder.is_operator_tier
    assert agent_comm._is_operator_tier is principal_builder.is_operator_tier


def test_operator_tier_predicate_consistent_for_admin_manager() -> None:
    """The admin-labelled manager is classified identically by both
    authorization surfaces.

    RED before candidate B: ``authorize._is_operator_tier`` returned
    False (no ``system.config.write`` cap) while
    ``agent_comm._is_operator_tier`` returned True (honoured the
    ``agent_id == "admin"`` label) for the SAME Principal.
    """
    p = _admin_labelled_manager()
    assert authorize._is_operator_tier(p) == agent_comm._is_operator_tier(p)
    # The reconciled value keeps the harness's admin-label contract.
    assert is_operator_tier(p) is True


def test_operator_tier_predicate_excludes_plain_manager_and_viewer() -> None:
    """A non-admin manager bearer and a viewer operator are NOT
    operator-tier (regression guard on the reconciled definition)."""
    manager = Principal(
        kind="agent_bearer", user_id=None, agent_id="w1", sysadmin=False,
        project_name=None, project_role=None, agent_role="manager",
        can_wake_loop=False, source_token="t",
    )
    viewer = Principal(
        kind="forwarding_header", user_id="op", agent_id=None, sysadmin=False,
        project_name=None, project_role="viewer", agent_role=None,
        can_wake_loop=False, source_token=None,
    )
    assert is_operator_tier(manager) is False
    assert is_operator_tier(viewer) is False


# ── Capability resolution: one path across every construction site ──


def test_agent_bearer_caps_identical_across_construction_sites(
    monkeypatch,
) -> None:
    """A given bearer yields the SAME capabilities frozenset whether the
    Principal is built by the shared builder, by ``authorize``'s fallback
    synthesizer, by ``agent_comm``'s fallback, or by the REST dispatch
    helper's bearer branch.
    """
    import agent_mcp.core.auth as auth
    import agent_mcp.core.globals as core_globals
    from agent_mcp.app._dispatch_helpers import _build_route_principal

    monkeypatch.setattr(auth, "get_agent_id", lambda tok: "agent-x")
    monkeypatch.setattr(
        core_globals, "active_agents", {"tok": {"agent_role": "manager"}}
    )

    built = build_agent_bearer_principal("tok")
    synth = authorize._synthesize_principal_from_arguments({"token": "tok"})
    comm = agent_comm._resolve_principal({"token": "tok"}, None)
    rest = _build_route_principal(
        bearer_token="tok", operator_session=False, operator_user_id=None,
    )

    caps = built.capabilities
    assert caps  # manager bundle is non-empty
    assert synth.capabilities == caps
    assert comm.capabilities == caps
    assert rest.capabilities == caps


def test_agent_bearer_builder_uses_resolve_capabilities(monkeypatch) -> None:
    """The shared agent_bearer builder's caps ARE
    ``resolve_capabilities(...)`` for the identity — the single path."""
    import agent_mcp.core.auth as auth
    import agent_mcp.core.globals as core_globals

    monkeypatch.setattr(auth, "get_agent_id", lambda tok: "agent-x")
    monkeypatch.setattr(
        core_globals, "active_agents", {"tok": {"agent_role": "worker"}}
    )
    p = build_agent_bearer_principal("tok")
    assert p.capabilities == resolve_capabilities(
        user_id=None, agent_id="agent-x", sysadmin=False,
        agent_role="worker", project_role=None, kind="agent_bearer",
    )


def test_operator_builder_uses_resolve_capabilities() -> None:
    """The operator/forwarding builder's caps ARE
    ``resolve_capabilities(...)`` — the one path that unions the
    project-role bundle with the group-capability overlay."""
    p = build_operator_principal(
        user_id="u1", kind="operator_session", project_role="operator",
        sysadmin=False,
    )
    assert p.capabilities == resolve_capabilities(
        user_id="u1", agent_id=None, sysadmin=False, agent_role=None,
        project_role="operator", kind="operator_session",
    )


def test_agent_bearer_builder_returns_none_for_no_bearer(monkeypatch) -> None:
    """No bearer / unresolvable bearer → None (caller surfaces its own
    unauthenticated outcome)."""
    import agent_mcp.core.auth as auth

    assert build_agent_bearer_principal(None) is None
    assert build_agent_bearer_principal("") is None
    monkeypatch.setattr(auth, "get_agent_id", lambda tok: None)
    assert build_agent_bearer_principal("bogus") is None
