"""Tests for ``agent_mcp.router.group_resolver`` (Phase 3 PR 3a).

The resolver owns three responsibilities:

  * ``add_group_member(group_id, member_user_id=..., member_group_id=...)``
    — validates exactly-one-set and runs cycle-detection BEFORE writing
    the edge. Raises ``CycleDetected`` if adding the new edge would
    close a cycle. (The DB has no FK that prevents cycles; the
    resolver is the canonical insert path so the table can't be
    poisoned by ordinary code paths.)

  * ``resolve_user_groups(user_id) -> set[str]`` — the transitive
    closure of group memberships the user belongs to (direct + via
    chains of nested groups).

  * ``resolve_user_is_sysadmin(user_id) -> bool`` — true if
    ``users.is_sysadmin`` OR any group in
    ``resolve_user_groups(user_id)`` has ``is_sysadmin=1``.

  * ``resolve_user_project_role(user_id, project_name) ->
    Optional[Literal["operator", "viewer"]]`` — walks
    ``project_membership`` rows for the user OR any of their groups
    (resolved via ``resolve_user_groups``). When multiple matches
    exist, returns the highest tier (operator > viewer). Returns
    ``None`` when no membership covers the user for that project.

These tests poke the resolver against a fresh router.db with the
Phase 3 schema applied; the bootstrap helper that flips the first
operator to sysadmin is exercised in ``test_p3_schema.py``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def router_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    projects_file = tmp_path / "projects.local.json"
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))
    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
        "agent_mcp.router.project_registry",
        "agent_mcp.router.group_resolver",
    ]:
        sys.modules.pop(mod, None)
    return db_path


@pytest.fixture
def identity(router_db: Path):
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()
    return identity


@pytest.fixture
def resolver(identity):
    import agent_mcp.router.group_resolver as group_resolver

    importlib.reload(group_resolver)
    return group_resolver


def _make_group(
    identity, group_id: str, name: str, is_sysadmin: bool = False
) -> None:
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, ?, '2026-06-18T00:00:00')",
            (group_id, name, 1 if is_sysadmin else 0),
        )


# ── add_group_member — input validation ─────────────────────────────


def test_add_group_member_requires_exactly_one(resolver, identity) -> None:
    _make_group(identity, "g1", "team")
    uid = identity.create_user(username="alice", password="pw")
    with pytest.raises(ValueError):
        resolver.add_group_member("g1")  # neither
    with pytest.raises(ValueError):
        resolver.add_group_member(
            "g1", member_user_id=uid, member_group_id="g1"
        )  # both


def test_add_group_member_user_happy_path(resolver, identity) -> None:
    _make_group(identity, "g1", "team")
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g1", member_user_id=uid)
    groups = resolver.resolve_user_groups(uid)
    assert groups == {"g1"}


def test_add_group_member_group_happy_path(resolver, identity) -> None:
    _make_group(identity, "g_parent", "parent")
    _make_group(identity, "g_child", "child")
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g_child", member_user_id=uid)
    resolver.add_group_member("g_parent", member_group_id="g_child")
    # alice ∈ g_child ∈ g_parent → resolve sees both
    assert resolver.resolve_user_groups(uid) == {"g_child", "g_parent"}


# ── Cycle detection ─────────────────────────────────────────────────


def test_cycle_detection_rejects_self_loop(resolver, identity) -> None:
    _make_group(identity, "g1", "team")
    with pytest.raises(resolver.CycleDetected):
        resolver.add_group_member("g1", member_group_id="g1")


def test_cycle_detection_rejects_two_cycle(resolver, identity) -> None:
    """A → B; adding B → A would form a cycle."""
    _make_group(identity, "ga", "team_a")
    _make_group(identity, "gb", "team_b")
    resolver.add_group_member("ga", member_group_id="gb")  # gb ∈ ga
    with pytest.raises(resolver.CycleDetected):
        # Trying to add ga ∈ gb would close the loop ga→gb→ga.
        resolver.add_group_member("gb", member_group_id="ga")


def test_cycle_detection_rejects_three_cycle(resolver, identity) -> None:
    """A → B → C; adding C → A would form a 3-cycle."""
    _make_group(identity, "ga", "a")
    _make_group(identity, "gb", "b")
    _make_group(identity, "gc", "c")
    resolver.add_group_member("ga", member_group_id="gb")  # gb ∈ ga
    resolver.add_group_member("gb", member_group_id="gc")  # gc ∈ gb
    with pytest.raises(resolver.CycleDetected):
        resolver.add_group_member("gc", member_group_id="ga")


def test_cycle_detection_allows_dag(resolver, identity) -> None:
    """Diamond shape is a DAG, not a cycle — should be allowed."""
    _make_group(identity, "top", "top")
    _make_group(identity, "mid_a", "mid_a")
    _make_group(identity, "mid_b", "mid_b")
    _make_group(identity, "leaf", "leaf")
    # leaf ∈ mid_a, leaf ∈ mid_b, mid_a ∈ top, mid_b ∈ top — diamond.
    resolver.add_group_member("mid_a", member_group_id="leaf")
    resolver.add_group_member("mid_b", member_group_id="leaf")
    resolver.add_group_member("top", member_group_id="mid_a")
    resolver.add_group_member("top", member_group_id="mid_b")
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("leaf", member_user_id=uid)
    assert resolver.resolve_user_groups(uid) == {
        "leaf",
        "mid_a",
        "mid_b",
        "top",
    }


# ── resolve_user_groups: transitive closure ─────────────────────────


def test_resolve_user_groups_three_level_nesting(resolver, identity) -> None:
    """g3 ∈ g2 ∈ g1; user ∈ g3 → resolve returns {g1, g2, g3}."""
    _make_group(identity, "g1", "level1")
    _make_group(identity, "g2", "level2")
    _make_group(identity, "g3", "level3")
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g3", member_user_id=uid)
    resolver.add_group_member("g2", member_group_id="g3")
    resolver.add_group_member("g1", member_group_id="g2")
    assert resolver.resolve_user_groups(uid) == {"g1", "g2", "g3"}


def test_resolve_user_groups_no_membership(resolver, identity) -> None:
    uid = identity.create_user(username="alice", password="pw")
    assert resolver.resolve_user_groups(uid) == set()


def test_resolve_user_groups_unknown_user(resolver, identity) -> None:
    # An ID that doesn't match any user / membership should resolve
    # to an empty set rather than crash.
    assert resolver.resolve_user_groups("ffffffffffffffff") == set()


# ── resolve_user_is_sysadmin ───────────────────────────────────────


def test_sysadmin_direct_flag(resolver, identity) -> None:
    uid = identity.create_user(username="alice", password="pw")
    with identity._connect() as conn:
        conn.execute("UPDATE users SET is_sysadmin = 1 WHERE user_id = ?", (uid,))
    assert resolver.resolve_user_is_sysadmin(uid) is True


def test_sysadmin_via_direct_group(resolver, identity) -> None:
    _make_group(identity, "g_admins", "admins", is_sysadmin=True)
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g_admins", member_user_id=uid)
    assert resolver.resolve_user_is_sysadmin(uid) is True


def test_sysadmin_via_nested_group(resolver, identity) -> None:
    """Sysadmin-ness inherits through nested groups."""
    _make_group(identity, "g_admins", "admins", is_sysadmin=True)
    _make_group(identity, "g_eng", "engineering", is_sysadmin=False)
    _make_group(identity, "g_sre", "sre", is_sysadmin=False)
    uid = identity.create_user(username="alice", password="pw")
    # alice ∈ g_sre ∈ g_eng ∈ g_admins
    resolver.add_group_member("g_sre", member_user_id=uid)
    resolver.add_group_member("g_eng", member_group_id="g_sre")
    resolver.add_group_member("g_admins", member_group_id="g_eng")
    assert resolver.resolve_user_is_sysadmin(uid) is True


def test_sysadmin_false_without_path(resolver, identity) -> None:
    # Phase 3 Wave 2 (v5.0.69): the FIRST user auto-becomes sysadmin
    # (so a fresh deployment always has a working sysadmin). Seed
    # an unrelated first user so alice — the user we want to
    # observe as non-sysadmin — lands as user #2.
    identity.create_user(username="__first_op", password="firstoperatorpw")
    _make_group(identity, "g_users", "users", is_sysadmin=False)
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g_users", member_user_id=uid)
    assert resolver.resolve_user_is_sysadmin(uid) is False


# ── resolve_user_project_role ──────────────────────────────────────


def test_resolve_user_project_role_via_direct_user(resolver, identity) -> None:
    uid = identity.create_user(username="alice", password="pw")
    identity.add_project_membership(uid, "proj_a")
    # Phase 1 helper inserts with default role='operator'.
    assert resolver.resolve_user_project_role(uid, "proj_a") == "operator"


def test_resolve_user_project_role_none_when_no_membership(
    resolver, identity
) -> None:
    uid = identity.create_user(username="alice", password="pw")
    assert resolver.resolve_user_project_role(uid, "no_such_project") is None


def test_resolve_user_project_role_via_group(resolver, identity) -> None:
    _make_group(identity, "g_team", "team")
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g_team", member_user_id=uid)
    # Group-based viewer membership.
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, group_id, role) "
            "VALUES ('proj_a', NULL, 'g_team', 'viewer')"
        )
    assert resolver.resolve_user_project_role(uid, "proj_a") == "viewer"


def test_resolve_user_project_role_operator_beats_viewer(
    resolver, identity
) -> None:
    """When both a viewer-tier and an operator-tier membership grant
    access, the resolver returns ``operator`` (highest tier)."""
    _make_group(identity, "g_viewers", "viewers")
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g_viewers", member_user_id=uid)
    # Direct operator row.
    identity.add_project_membership(uid, "proj_a")
    # Group-based viewer row on the same project.
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, group_id, role) "
            "VALUES ('proj_a', NULL, 'g_viewers', 'viewer')"
        )
    assert resolver.resolve_user_project_role(uid, "proj_a") == "operator"


def test_resolve_user_project_role_via_nested_group(resolver, identity) -> None:
    """Transitive group membership grants role too."""
    _make_group(identity, "g_outer", "outer")
    _make_group(identity, "g_inner", "inner")
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g_inner", member_user_id=uid)
    resolver.add_group_member("g_outer", member_group_id="g_inner")
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, group_id, role) "
            "VALUES ('proj_a', NULL, 'g_outer', 'viewer')"
        )
    assert resolver.resolve_user_project_role(uid, "proj_a") == "viewer"
