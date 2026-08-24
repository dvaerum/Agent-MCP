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
from agent_mcp.app.rest_principal import RestPrincipal
from agent_mcp.core.capabilities import resolve_capabilities
from agent_mcp.core.principal import Principal
from agent_mcp.core.principal_builder import (
    build_agent_bearer_principal,
    build_operator_principal,
    is_operator_tier,
)
from tests.harness import make_principal, with_bearer


def _admin_labelled_manager() -> Principal:
    """The harness's manager-role row labelled ``admin`` — the exact
    identity whose classification the two predicate copies disagreed on.
    """
    return make_principal(
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
    manager = make_principal(
        kind="agent_bearer", user_id=None, agent_id="w1", sysadmin=False,
        project_name=None, project_role=None, agent_role="manager",
        can_wake_loop=False, source_token="t",
    )
    viewer = make_principal(
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
    # token-retirement PR 2 (Phase B): the two fallbacks source the
    # bearer from the ``request_auth_token`` ContextVar, not
    # ``arguments["token"]``. Make the bearer visible via the same seam
    # the middleware / harness set.
    with with_bearer("tok"):
        synth = authorize._synthesize_principal_from_arguments({})
        comm = agent_comm._resolve_principal({}, None)
    rest = _build_route_principal(bearer_token="tok")

    caps = built.capabilities
    assert caps  # manager bundle is non-empty
    assert synth.capabilities == caps
    assert comm.capabilities == caps
    assert rest.capabilities == caps


def test_route_principal_threads_project_name_for_operator_session() -> None:
    """Finding B (security-arch-hardening-consolidated.md Phase 1):
    ``_build_route_principal`` must accept and thread ``project_name``
    for the operator_session shape, the same way it already threads
    ``project_role``/``sysadmin``. Before this fix, the only per-project
    REST route needing project_name (agents.py's register route) had to
    build its own ``operator_session`` Principal inline via
    ``build_operator_principal`` directly instead of routing through
    this shared helper -- a 20-line duplicate of AZ-R14-1's forwarding-
    role threading that this fix deletes.
    """
    from agent_mcp.app._dispatch_helpers import _build_route_principal

    built = _build_route_principal(
        auth=RestPrincipal(kind="session", user={"username": "op-1"}),
        project_name="demo-project",
    )
    assert built is not None
    assert built.project_name == "demo-project"


def test_route_principal_project_name_defaults_to_none() -> None:
    """Regression: every existing call site that doesn't pass
    project_name (memories.py, settings.py, tasks.py, messages.py,
    schedules.py, composition.py) must keep getting None, unchanged."""
    from agent_mcp.app._dispatch_helpers import _build_route_principal

    built = _build_route_principal(
        auth=RestPrincipal(kind="session", user={"username": "op-1"}),
    )
    assert built is not None
    assert built.project_name is None


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


# ── arch-r5 #1: capabilities is required — no silent back-fill ──────


def test_principal_requires_capabilities_no_back_fill() -> None:
    """``Principal(...)`` without ``capabilities=`` now raises
    ``TypeError`` — the Wave 9 PR 0 ``__post_init__`` back-fill (which
    resolved caps from identity fields when the caller omitted
    ``capabilities=``) is gone. Structurally closes the class of bug
    this file's RED cases below exercise: there is no construction
    path left that can silently resolve a caps set without going
    through :func:`resolve_capabilities` (or a builder that wraps it)
    explicitly.
    """
    import pytest

    with pytest.raises(TypeError):
        Principal(  # type: ignore[call-arg]
            kind="agent_bearer",
            user_id=None,
            agent_id="x",
            sysadmin=False,
            project_name=None,
            project_role=None,
            agent_role=None,
            can_wake_loop=False,
            source_token=None,
        )


def test_group_privileged_identity_builder_matches_direct_resolver(
    monkeypatch,
) -> None:
    """arch-r5 #1 — the property the deleted back-fill made impossible
    to hold: for a group-privileged identity, the builder's caps and a
    direct :func:`resolve_capabilities` call with the SAME ``groups=``
    are bit-for-bit identical (exactly one resolution path), AND a
    resolution that can't see the caller's groups — the shape the
    ``__post_init__`` back-fill was permanently stuck in, since a bare
    ``Principal(...)`` call has no way to thread ``groups=`` through —
    is a STRICT SUBSET of the full resolution.

    RED before this candidate: the back-fill called
    ``resolve_capabilities(...)`` from ``Principal.__post_init__``
    WITHOUT a ``groups=`` argument (it had no way to receive one — a
    bare dataclass constructor call carries no group context). Any
    caller that built a Principal directly for a group-privileged
    identity — instead of going through
    :func:`build_operator_principal` — silently got the narrower,
    groups-blind set below (``blind``) while believing it had the
    caller's full grant (``built``). ``capabilities`` being required
    now makes that impossible: you cannot construct a Principal at all
    without deciding how to resolve caps, so the narrower path can no
    longer happen silently.
    """
    import agent_mcp.repositories.group_capability_repository as gcr
    import agent_mcp.router.group_resolver as gr

    # Model a router.db-blind resolution context — self-resolving via
    # ``resolve_user_groups`` (what a bare/back-filled Principal was
    # stuck with) finds nothing for this identity.
    monkeypatch.setattr(gr, "resolve_user_groups", lambda user_id: set())
    monkeypatch.setattr(
        gcr,
        "fetch",
        lambda gid: frozenset({"system.groups.manage"}) if gid == "g1" else frozenset(),
    )

    # The caller (e.g. the router auth middleware) already resolved
    # the identity's transitive group set through its own means and
    # threads it through explicitly.
    groups = {"g1"}

    built = build_operator_principal(
        user_id="grouped-operator",
        kind="operator_session",
        project_role="viewer",
        sysadmin=False,
        groups=groups,
    )
    direct = resolve_capabilities(
        user_id="grouped-operator",
        agent_id=None,
        sysadmin=False,
        agent_role=None,
        project_role="viewer",
        kind="operator_session",
        groups=groups,
    )
    assert built.capabilities == direct
    assert "system.groups.manage" in built.capabilities

    # The groups-blind shape: no groups= passed at all — self-resolve
    # via (the monkeypatched) resolve_user_groups, which finds nothing
    # for this identity. This is exactly what the deleted
    # __post_init__ back-fill produced for every bare Principal(...)
    # call — it could never pass groups= because it had none to pass.
    blind = resolve_capabilities(
        user_id="grouped-operator",
        agent_id=None,
        sysadmin=False,
        agent_role=None,
        project_role="viewer",
        kind="operator_session",
    )
    assert blind < built.capabilities, (
        "a groups-blind resolution must be a STRICT SUBSET of the "
        "groups-aware one for a group-privileged identity — this is "
        "the gap the deleted __post_init__ back-fill silently opened"
    )
    assert "system.groups.manage" not in blind
