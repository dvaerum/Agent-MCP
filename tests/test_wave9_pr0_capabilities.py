"""Wave 9 PR 0 — capability-foundation smoke tests.

Wave 9 PR 0 of 7 in ``prancy-napping-pie.md``. Pins:

* The :data:`KNOWN_CAPABILITIES` set is exactly the 28-element
  vocabulary the plan locks; the per-resource × verb regex shape
  ``^[a-z]+(\\.[a-z_]+)+$`` holds for every cap.
* :data:`PROJECT_ROLE_BUNDLES` and :data:`AGENT_ROLE_BUNDLES` are
  strict subsets of :data:`KNOWN_CAPABILITIES` — no typos, no
  cap-by-omission.
* :meth:`Principal.has_capability` honours the sysadmin wildcard
  short-circuit, the per-cap presence check, AND the project-
  membership gate for non-``system.*`` caps.
* :func:`tests.harness.with_capabilities` produces a Principal whose
  cap set matches the helper's input verbatim.

Wave 9 PR 6 deleted the legacy ``has_role()`` bridge + the
``ROLE_TO_CAPS`` bridge map, so the bridge-preservation tests this
file used to carry are gone — capabilities are the single surface.
"""
from __future__ import annotations

import re

import pytest

from agent_mcp.core.capabilities import (
    AGENT_ROLE_BUNDLES,
    KNOWN_CAPABILITIES,
    PROJECT_ROLE_BUNDLES,
    SYSADMIN_WILDCARD,
    resolve_capabilities,
)
from tests.harness import make_principal, with_capabilities

# ── Vocabulary shape ───────────────────────────────────────────────


#: Locked vocabulary per the Wave 9 design (2026-06-30 grilling
#: locked these 27 strings). Re-stating the set here (instead of
#: importing) so a typo in capabilities.py is caught by a direct
#: equality check rather than the smoke tests passing trivially.
_EXPECTED_KNOWN_CAPABILITIES: frozenset[str] = frozenset({
    "mcp.connect",
    "agents.view",
    "agents.register",
    "agents.terminate",
    "agents.use",
    "tasks.view",
    "tasks.create",
    "tasks.update",
    "tasks.delete",
    "tasks.assign",
    "memories.view",
    "memories.create",
    "memories.update",
    "memories.delete",
    "messages.view",
    "messages.send",
    "files.use",
    "coordination.assist",
    "coordination.wait",
    "rag.query",
    "rag.rebuild",
    "system.view",
    "system.config.write",
    "system.users.manage",
    "system.groups.manage",
    "system.groups.capabilities.manage",
    "system.projects.manage",
    "system.sso.configure",
})


_CAP_REGEX = re.compile(r"^[a-z]+(\.[a-z_]+)+$")


def test_known_capabilities_matches_locked_vocabulary():
    """KNOWN_CAPABILITIES is exactly the vocabulary the Wave 9 plan
    enumerates. Adding / removing entries is a design change outside
    Wave 9 PR 0 scope.

    Note: the plan's prose says "27 capabilities total" but the
    explicit enumeration (both in the section header bundle list and
    in this PR's prompt) lists 28 cap strings. The explicit list is
    the authoritative content; the count in the prose is an off-by-
    one in the design doc. The test pins the explicit set.
    """
    assert len(_EXPECTED_KNOWN_CAPABILITIES) == 28
    assert KNOWN_CAPABILITIES == _EXPECTED_KNOWN_CAPABILITIES


def test_every_capability_string_matches_resource_verb_regex():
    """Every cap is per-resource × verb shaped (lowercase, dotted,
    no trailing/leading dots, no upper-case). The regex catches the
    common typos (``System.View``, ``mcpConnect``,
    ``rag-rebuild``)."""
    for cap in KNOWN_CAPABILITIES:
        assert _CAP_REGEX.match(cap), (
            f"capability {cap!r} does not match per-resource × verb regex"
        )


@pytest.mark.parametrize("bundle_name", sorted(PROJECT_ROLE_BUNDLES))
def test_project_role_bundles_are_strict_subsets(bundle_name: str):
    """Every cap a project_role bundle grants is a member of
    KNOWN_CAPABILITIES — no typo'd grants."""
    diff = PROJECT_ROLE_BUNDLES[bundle_name] - KNOWN_CAPABILITIES
    assert not diff, (
        f"PROJECT_ROLE_BUNDLES[{bundle_name!r}] grants unknown caps: "
        f"{sorted(diff)}"
    )


@pytest.mark.parametrize("bundle_name", sorted(AGENT_ROLE_BUNDLES))
def test_agent_role_bundles_are_strict_subsets(bundle_name: str):
    """Every cap an agent_role bundle grants is a member of
    KNOWN_CAPABILITIES — no typo'd grants."""
    diff = AGENT_ROLE_BUNDLES[bundle_name] - KNOWN_CAPABILITIES
    assert not diff, (
        f"AGENT_ROLE_BUNDLES[{bundle_name!r}] grants unknown caps: "
        f"{sorted(diff)}"
    )


# ── has_capability semantics ───────────────────────────────────────


def test_sysadmin_wildcard_admits_every_cap():
    """``has_capability`` short-circuits on the wildcard sentinel; a
    sysadmin admits ``"any"`` (and any other arbitrary string) without
    materialising the full 27-cap set onto the Principal."""
    p = make_principal(
        kind="operator_session",
        user_id="alice",
        agent_id=None,
        sysadmin=True,
        project_name="proj-a",
        project_role=None,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )
    # make_principal resolves sysadmin -> wildcard set via resolve_capabilities.
    assert SYSADMIN_WILDCARD in p.capabilities
    assert p.has_capability("any")
    assert p.has_capability("agents.create")  # not even a real cap
    assert p.has_capability("system.users.manage")


def test_operator_with_project_role_operator_admits_resource_cap():
    """An operator-session caller with project_role='operator' has the
    PROJECT_ROLE_BUNDLES['operator'] bundle, which includes
    ``agents.register``. Resource cap admits because project_role is
    set (non-``system.*`` gate)."""
    p = with_capabilities(*PROJECT_ROLE_BUNDLES["operator"])
    assert p.project_role == "operator"
    assert p.has_capability("agents.register")
    # Project-bundle is bracketing for the gate, so explicitly try
    # both a present-and-resource-shaped cap (passes) and an
    # absent-from-bundle cap (denies).
    assert not p.has_capability("system.users.manage")


def test_operator_with_project_role_viewer_denies_write_cap():
    """A viewer's bundle covers reads only — ``agents.register`` is
    not in PROJECT_ROLE_BUNDLES['viewer'] so ``has_capability`` denies
    regardless of project_role."""
    p = make_principal(
        kind="operator_session",
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        project_name="proj-a",
        project_role="viewer",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )
    assert "agents.register" not in p.capabilities
    assert not p.has_capability("agents.register")
    assert p.has_capability("agents.view")  # viewer's bundle includes view


def test_sysadmin_admits_system_users_manage_regardless_of_project_role():
    """``system.*`` caps don't require project membership AND the
    sysadmin wildcard short-circuits the cap-set check anyway."""
    p = make_principal(
        kind="operator_session",
        user_id="alice",
        agent_id=None,
        sysadmin=True,
        project_name=None,
        project_role=None,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )
    assert p.has_capability("system.users.manage")


def test_operator_with_project_role_only_denies_system_users_manage():
    """``system.users.manage`` is sysadmin-only; an operator with a
    project-role bundle (no ``system.users.manage`` cap) is denied
    even though every other guard would admit them."""
    p = with_capabilities(*PROJECT_ROLE_BUNDLES["operator"])
    assert "system.users.manage" not in p.capabilities
    assert not p.has_capability("system.users.manage")


def test_has_capability_denies_unknown_cap():
    """Default-deny on unknown cap strings — typos / vocabulary
    drift are rejected silently at the gate (and surfaced at code-
    review via the KNOWN_CAPABILITIES smoke test)."""
    p = with_capabilities(*PROJECT_ROLE_BUNDLES["operator"])
    assert not p.has_capability("typo.cap.that.does.not.exist")


def test_has_capability_denies_resource_cap_without_project_role():
    """The per-cap project-membership gate denies a resource cap when
    the caller has no project membership AND isn't an agent_bearer.
    Models the forwarding-header case where the per-project backend
    has no router.db handle to resolve project role."""
    p = make_principal(
        kind="forwarding_header",
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
        # Force a cap present in the set: the per-cap gate denies
        # because project_role is None AND kind is not agent_bearer.
        capabilities=frozenset({"agents.register"}),
    )
    assert "agents.register" in p.capabilities
    assert not p.has_capability("agents.register")


# ── resolve_capabilities resolution chain ──────────────────────────


def test_resolve_capabilities_sysadmin_returns_wildcard_set():
    caps = resolve_capabilities(
        user_id="alice",
        agent_id=None,
        sysadmin=True,
        agent_role=None,
        project_role=None,
        kind="operator_session",
    )
    assert caps == frozenset({SYSADMIN_WILDCARD})


def test_resolve_capabilities_agent_bearer_returns_role_bundle():
    """Agent-bearer caps come from AGENT_ROLE_BUNDLES alone — no
    group lookup, no project-role bundle (agents aren't operators)."""
    caps = resolve_capabilities(
        user_id=None,
        agent_id="alice-wkr",
        sysadmin=False,
        agent_role="worker",
        project_role=None,
        kind="agent_bearer",
    )
    assert caps == AGENT_ROLE_BUNDLES["worker"]


def test_resolve_capabilities_operator_session_with_project_role():
    """Operator-session resolves to PROJECT_ROLE_BUNDLES[project_role]
    when no router.db is available (the group-cap overlay returns
    empty)."""
    caps = resolve_capabilities(
        user_id=None,  # no user → no group lookup attempted
        agent_id=None,
        sysadmin=False,
        agent_role=None,
        project_role="operator",
        kind="operator_session",
    )
    assert caps == PROJECT_ROLE_BUNDLES["operator"]


def test_resolve_capabilities_agent_bearer_no_role_is_empty():
    """An agent with no role (DB drift, pre-migration row) gets an
    empty cap set — default-deny matches the pre-Wave-9 contract for
    a malformed agent_role column."""
    caps = resolve_capabilities(
        user_id=None,
        agent_id="alice",
        sysadmin=False,
        agent_role=None,
        project_role=None,
        kind="agent_bearer",
    )
    assert caps == frozenset()


# ── resolve_capabilities: group caps are sanitised (Finding 2) ─────


def _patch_group_caps(monkeypatch, group_caps):
    """Point resolve_capabilities' group lookup at ``group_caps``.

    Patches the source symbols (imported lazily inside the function)
    so a single group ``"g1"`` resolves to the given cap frozenset.
    """
    import agent_mcp.repositories.group_capability_repository as gcr
    import agent_mcp.router.group_resolver as gr

    monkeypatch.setattr(gr, "resolve_user_groups", lambda user_id: {"g1"})
    monkeypatch.setattr(gcr, "fetch", lambda gid: frozenset(group_caps))


def test_resolve_capabilities_drops_wildcard_from_group_data(monkeypatch):
    """SEC (Finding 2): the sysadmin wildcard ``"*"`` must NEVER be
    sourced from group data.

    Only the ``sysadmin=True`` branch may mint the wildcard. If a
    group_capability row somehow contains ``"*"`` (migration bug,
    repair script, direct SQL), unioning it verbatim would silently
    make every group member a sysadmin. resolve_capabilities must drop
    it — the caller stays a plain operator, not a sysadmin.
    """
    _patch_group_caps(monkeypatch, {SYSADMIN_WILDCARD, "tasks.assign"})

    caps = resolve_capabilities(
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        agent_role=None,
        project_role="viewer",
        kind="operator_session",
    )

    assert SYSADMIN_WILDCARD not in caps
    # The legitimate, KNOWN cap from the group still comes through.
    assert "tasks.assign" in caps
    # And the wildcard did not smuggle in blanket admit.
    p = make_principal(
        kind="operator_session",
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        project_name="proj",
        project_role="viewer",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
        capabilities=caps,
    )
    assert not p.has_capability("system.users.manage")


def test_resolve_capabilities_drops_unknown_group_capability(monkeypatch):
    """A bogus / typo'd capability string from group data is dropped —
    only members of KNOWN_CAPABILITIES survive the union."""
    _patch_group_caps(
        monkeypatch, {"tasks.asssign_typo", "not.a.real.cap", "memories.view"}
    )

    caps = resolve_capabilities(
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        agent_role=None,
        project_role="viewer",
        kind="operator_session",
    )

    assert "tasks.asssign_typo" not in caps
    assert "not.a.real.cap" not in caps
    # The one real cap survives.
    assert "memories.view" in caps


# ── with_capabilities harness helper ───────────────────────────────


def test_with_capabilities_helper_produces_expected_cap_set():
    p = with_capabilities("tasks.assign", "memories.update")
    assert p.capabilities == frozenset({"tasks.assign", "memories.update"})
    assert p.has_capability("tasks.assign")
    assert p.has_capability("memories.update")
    assert not p.has_capability("agents.register")
    # Helper produces an operator-shaped Principal so the per-cap
    # project-membership gate admits resource caps.
    assert p.project_role == "operator"
    assert p.kind == "operator_session"


def test_with_capabilities_empty_set_produces_default_deny():
    p = with_capabilities()
    assert p.capabilities == frozenset()
    assert not p.has_capability("tasks.view")
    # Even though project_role is operator, no cap means no admit.
    assert not p.has_capability("agents.register")
