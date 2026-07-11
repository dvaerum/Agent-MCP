"""Arch-deepening R2 #1a — the ``RouterStore`` connection-injection seam.

The router persisted users / groups / membership with THREE incompatible
connection helpers and forked its graph traversals inside
``admin_users_api`` (``_group_and_ancestors`` ≈ ``resolve_user_groups``,
``_group_is_transitively_sysadmin`` ≈ ``resolve_user_is_sysadmin``,
``_group_resolved_project_roles`` ≈ ``resolve_user_project_role``) precisely
because ``group_resolver`` owned its own connection: a handler running inside
``BEGIN IMMEDIATE`` could not call the resolver without the resolver opening a
SECOND connection and breaking snapshot isolation.

This file pins the invariant that closes that fork: the ``RouterStore``
group-resolution methods take ``conn=None`` and, when handed the caller's
open transactional connection, MUST NOT open a second one — and the
group-rooted (previously forked) answers equal the canonical user-rooted
``group_resolver`` answers.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest


# ── Fixtures (mirror tests/test_group_resolver.py) ──────────────────


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
        "agent_mcp.router.router_store",
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


@pytest.fixture
def store(resolver):
    import agent_mcp.router.router_store as router_store

    importlib.reload(router_store)
    return router_store.store


def _make_group(
    identity, group_id: str, name: str, is_sysadmin: bool = False
) -> None:
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, ?, '2026-06-18T00:00:00')",
            (group_id, name, 1 if is_sysadmin else 0),
        )


def _add_project_membership(
    identity, project_name: str, role: str, *, user_id=None, group_id=None
) -> None:
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, group_id, role) "
            "VALUES (?, ?, ?, ?)",
            (project_name, user_id, group_id, role),
        )


# ── Invariant 1: a handler's open transaction never triggers a 2nd open ──


def test_store_methods_enlist_in_open_conn_without_self_opening(
    store, resolver, identity, monkeypatch
) -> None:
    """The keystone invariant: pass the store the caller's connection and it
    must run every group-resolution query on THAT connection — never open a
    second one (which under ``BEGIN IMMEDIATE`` would deadlock / read a stale
    snapshot). We prove it by making a self-open explode, then driving the
    store entirely through a caller-owned connection."""
    _make_group(identity, "g_admins", "admins", is_sysadmin=True)
    _make_group(identity, "g_eng", "eng")
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g_eng", member_user_id=uid)
    resolver.add_group_member("g_admins", member_group_id="g_eng")
    _add_project_membership(
        identity, "proj_a", "operator", group_id="g_eng"
    )

    # Capture the autocommit-path answers first (conn=None self-opens).
    groups_ac = store.resolve_user_groups(uid)
    sysadmin_ac = store.resolve_user_is_sysadmin(uid)
    role_ac = store.resolve_user_project_role(uid, "proj_a")
    ancestors_ac = store.resolve_group_ancestors("g_eng")
    grp_sysadmin_ac = store.group_is_transitively_sysadmin("g_eng")
    grp_roles_ac = store.group_resolved_project_roles("g_eng")

    # From here on ANY self-open is a bug: detonate it.
    def _boom(*_a, **_k):
        raise AssertionError(
            "RouterStore opened a second connection while handed an open one"
        )

    monkeypatch.setattr(identity, "_connect", _boom)

    # A handler holds its own connection inside BEGIN IMMEDIATE.
    conn = sqlite3.connect(str(identity.get_router_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert store.resolve_user_groups(uid, conn=conn) == groups_ac
        assert store.resolve_user_is_sysadmin(uid, conn=conn) == sysadmin_ac
        assert (
            store.resolve_user_project_role(uid, "proj_a", conn=conn)
            == role_ac
        )
        assert store.resolve_group_ancestors("g_eng", conn=conn) == ancestors_ac
        assert (
            store.group_is_transitively_sysadmin("g_eng", conn=conn)
            == grp_sysadmin_ac
        )
        assert (
            store.group_resolved_project_roles("g_eng", conn=conn)
            == grp_roles_ac
        )
        assert store.would_create_cycle("g_eng", "g_admins", conn=conn) is True
        conn.execute("COMMIT")
    finally:
        conn.close()


# ── Invariant 2: group-rooted answers == canonical user-rooted answers ──


def test_group_rooted_traversal_equals_user_rooted_resolution(
    store, resolver, identity
) -> None:
    """A fresh member of ``group_id`` inherits exactly what the group-rooted
    traversal reports — which is why the ``admin_users_api`` forks were
    redundant. Assert the group-rooted store methods equal the user-rooted
    resolver answers for a user placed solely in that group."""
    _make_group(identity, "g_admins", "admins", is_sysadmin=True)
    _make_group(identity, "g_eng", "eng")
    resolver.add_group_member("g_admins", member_group_id="g_eng")
    _add_project_membership(identity, "proj_a", "operator", group_id="g_eng")
    _add_project_membership(identity, "proj_b", "viewer", group_id="g_admins")

    # A user whose ONLY membership is g_eng inherits g_eng's closure.
    uid = identity.create_user(username="alice", password="pw")
    resolver.add_group_member("g_eng", member_user_id=uid)

    # group-rooted sysadmin == user-rooted sysadmin
    assert (
        store.group_is_transitively_sysadmin("g_eng")
        == store.resolve_user_is_sysadmin(uid)
        is True
    )
    # group-rooted project roles == the per-project user-rooted role
    grp_roles = store.group_resolved_project_roles("g_eng")
    for project, role in grp_roles.items():
        assert store.resolve_user_project_role(uid, project) == role
    assert grp_roles == {"proj_a": "operator", "proj_b": "viewer"}
    # ancestors == the user's resolved group set
    assert store.resolve_group_ancestors("g_eng") == store.resolve_user_groups(
        uid
    )


def test_role_rank_is_single_sourced(store, resolver) -> None:
    """The role tier lives in ONE place now; the store exposes it."""
    assert store.role_rank("operator") > store.role_rank("viewer")
    assert store.role_rank("bogus") == 0
    # group_resolver's tier and the store's rank agree.
    assert resolver.role_rank("operator") == store.role_rank("operator")
