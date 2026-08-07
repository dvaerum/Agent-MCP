"""Arch-deepening R2 #1c — the RouterStore finisher.

#1a introduced the ``RouterStore`` connection-injection seam and moved the
forked group *reads* behind it; #1b closed the membership *write* forks and
the passwordless-user fork. #1c completes the consolidation, and these tests
pin the four invariants it must preserve:

  1. **First-operator bootstrap (SECURITY-critical).** On an otherwise-empty
     ``users`` table, the first user created becomes sysadmin AND gets
     ``project_membership`` in every registered project — and that invariant
     is applied via exactly ONE store-owned routine
     (``store.bootstrap_first_operator``), which ``create_user`` calls inside
     its own INSERT transaction. A SECOND user (table non-empty) is neither
     auto-sysadmin nor auto-granted membership.

  2. **One empty-table probe.** Every caller
     (``store.users_table_is_empty``, ``identity.users_table_is_empty``,
     ``setup_wizard.users_table_is_empty``, ``sso._users_table_is_empty``)
     agrees, and the probe reads inside a caller's open transaction when
     handed a ``conn``.

  3. **SSO reads/writes through the store.** The six former inline
     ``sqlite3.connect`` sites in ``sso`` now route through the store and the
     store methods preserve the exact query semantics.

  4. **One connection factory, both modes.** ``store.connect`` (raw /
     transactional), ``store.connection`` (autocommit self-open),
     ``identity._connect`` (autocommit ctx-mgr) and
     ``admin_users_api._connect`` (raw, for the handlers' BEGIN IMMEDIATE)
     all build on the single ``identity.open_connection`` factory — the
     "3 connection helpers → 1" payoff.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# ── Fixtures (mirror tests/router/test_arch_r2_1b_membership.py) ─────


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
        "agent_mcp.router.sso",
        "agent_mcp.router.setup_wizard",
        "agent_mcp.router.admin_users_api",
    ]:
        sys.modules.pop(mod, None)
    return db_path


@pytest.fixture
def mods(router_db: Path) -> SimpleNamespace:
    """Reload the whole router seam in dependency order off a fresh
    ``router.db`` so every module shares one ``identity`` module object
    (the connection-factory spy relies on this)."""
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()
    import agent_mcp.router.group_resolver as group_resolver

    importlib.reload(group_resolver)
    import agent_mcp.router.router_store as router_store

    importlib.reload(router_store)
    import agent_mcp.router.sso as sso

    importlib.reload(sso)
    import agent_mcp.router.setup_wizard as setup_wizard

    importlib.reload(setup_wizard)
    import agent_mcp.router.admin_users_api as admin_users_api

    importlib.reload(admin_users_api)
    return SimpleNamespace(
        identity=identity,
        group_resolver=group_resolver,
        store=router_store.store,
        sso=sso,
        setup_wizard=setup_wizard,
        admin_users_api=admin_users_api,
    )


def _register_project(name: str, tmp_path: Path) -> None:
    from agent_mcp.router.project_registry import ProjectRegistry

    ProjectRegistry().register(name, str(tmp_path / name))


def _project_names_for_user(identity, user_id: str) -> list[str]:
    with identity._connect() as conn:
        cur = conn.execute(
            "SELECT project_name FROM project_membership "
            "WHERE user_id = ? ORDER BY project_name",
            (user_id,),
        )
        return [r["project_name"] for r in cur.fetchall()]


# ── Invariant 1: first-operator bootstrap (SECURITY-critical) ───────


def test_first_operator_bootstrap_invariant(
    mods: SimpleNamespace, tmp_path: Path
) -> None:
    """Empty table → first user is sysadmin AND a member of every
    registered project. THE invariant, via the one code path."""
    _register_project("alpha", tmp_path)
    _register_project("beta", tmp_path)

    uid = mods.identity.create_user(username="alice", password="pw-123456789")

    row = mods.identity.get_user_by_id(uid)
    assert row is not None
    assert row["is_sysadmin"] == 1
    assert _project_names_for_user(mods.identity, uid) == ["alpha", "beta"]


def test_second_user_is_not_auto_bootstrapped(
    mods: SimpleNamespace, tmp_path: Path
) -> None:
    """Once the table is non-empty, a later user is neither auto-sysadmin
    nor auto-granted membership — the bootstrap fires for the first only."""
    _register_project("alpha", tmp_path)
    mods.identity.create_user(username="alice", password="pw-123456789")

    uid2 = mods.identity.create_user(username="bob", password="pw-123456789")
    row2 = mods.identity.get_user_by_id(uid2)
    assert row2["is_sysadmin"] == 0
    assert _project_names_for_user(mods.identity, uid2) == []


def test_first_operator_bootstrap_routes_through_single_store_routine(
    mods: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``create_user``'s first-user path applies the invariant via
    ``store.bootstrap_first_operator`` — exactly one owner."""
    _register_project("alpha", tmp_path)
    seen: list[tuple[str, bool]] = []
    real = mods.store.bootstrap_first_operator

    def spy(user_id: str, *, grant_sysadmin: bool, conn=None) -> None:
        seen.append((user_id, grant_sysadmin))
        return real(user_id, grant_sysadmin=grant_sysadmin, conn=conn)

    monkeypatch.setattr(mods.store, "bootstrap_first_operator", spy)

    uid = mods.identity.create_user(username="alice", password="pw-123456789")
    assert seen == [(uid, True)]


def test_bootstrap_opt_out_creates_first_user_without_sysadmin(
    mods: SimpleNamespace, tmp_path: Path
) -> None:
    """The SSO opt-out path (``bootstrap_sysadmin=False``) still grants
    all-project membership but NOT sysadmin — the deprovision-bootstrap
    security rule."""
    _register_project("alpha", tmp_path)
    uid = mods.identity.create_user(
        username="idp-user",
        password=None,
        password_hash=None,
        sso_subject="oidc:https://idp/:sub-1",
        bootstrap_sysadmin=False,
    )
    row = mods.identity.get_user_by_id(uid)
    assert row["is_sysadmin"] == 0
    assert row["password_hash"] is None
    assert _project_names_for_user(mods.identity, uid) == ["alpha"]


# ── Invariant 2: one empty-table probe ──────────────────────────────


def test_empty_probe_callers_all_agree(mods: SimpleNamespace) -> None:
    """Every empty-probe caller agrees, before and after the first user,
    via the single ``store.users_table_is_empty`` helper."""
    probes = [
        mods.store.users_table_is_empty,
        mods.identity.users_table_is_empty,
        mods.setup_wizard.users_table_is_empty,
        mods.sso._users_table_is_empty,
    ]
    assert all(p() is True for p in probes)

    mods.identity.create_user(username="alice", password="pw-123456789")

    assert all(p() is False for p in probes)


def test_empty_probe_enlists_in_open_transaction(
    mods: SimpleNamespace,
) -> None:
    """``users_table_is_empty(conn=...)`` reads inside the caller's own
    uncommitted transaction (create_user's first-user check depends on
    seeing its own INSERT)."""
    raw = mods.store.connect()
    try:
        assert mods.store.users_table_is_empty(conn=raw) is True
        raw.execute(
            "INSERT INTO users (user_id, username, created_at, is_sysadmin) "
            "VALUES ('u1', 'alice', '2026-01-01T00:00:00', 0)"
        )
        # The same connection sees its own uncommitted row.
        assert mods.store.users_table_is_empty(conn=raw) is False
    finally:
        raw.rollback()
        raw.close()


# ── Invariant 3: SSO reads/writes through the store ─────────────────


def test_store_find_user_by_sso_subject(mods: SimpleNamespace) -> None:
    subject = "oidc:https://idp/:sub-1"
    uid = mods.identity.create_user(
        username="idp-user",
        password=None,
        password_hash=None,
        sso_subject=subject,
        bootstrap_sysadmin=False,
    )
    found = mods.store.find_user_by_sso_subject(subject)
    assert found is not None and found["user_id"] == uid
    assert mods.store.find_user_by_sso_subject("oidc:nope") is None
    # The sso wrapper delegates to the exact same store method.
    assert mods.sso._find_user_by_subject(subject) == found


def test_store_find_linkable_user_by_email(mods: SimpleNamespace) -> None:
    """A password-backed operator is a link target; a passwordless SSO row
    (non-NULL sso_subject) is NOT — the account-takeover guard."""
    mods.identity.create_user(
        username="alice", password="pw-123456789", email="a@corp",
    )
    mods.identity.create_user(
        username="idp-only",
        password=None,
        password_hash=None,
        email="sso@corp",
        sso_subject="oidc:https://idp/:x",
    )
    linked = mods.store.find_linkable_user_by_email("A@CORP")
    assert linked is not None and linked["username"] == "alice"
    # passwordless subject-bearing row is excluded from linking
    assert mods.store.find_linkable_user_by_email("sso@corp") is None


def test_store_stamp_sso_subject_if_absent(mods: SimpleNamespace) -> None:
    uid = mods.identity.create_user(
        username="alice", password="pw-123456789",
    )
    mods.store.stamp_sso_subject_if_absent(uid, "oidc:iss:first")
    assert mods.identity.get_user_by_id(uid)["sso_subject"] == "oidc:iss:first"
    # Idempotent: a second, different subject can never overwrite.
    mods.store.stamp_sso_subject_if_absent(uid, "oidc:iss:second")
    assert mods.identity.get_user_by_id(uid)["sso_subject"] == "oidc:iss:first"


def test_store_ensure_group_is_idempotent(mods: SimpleNamespace) -> None:
    gid1 = mods.store.ensure_group("oidc:admins")
    assert gid1 is not None
    gid2 = mods.store.ensure_group("oidc:admins")
    assert gid2 == gid1  # existing group returned, not re-created


def test_store_group_membership_prefix_and_removal(
    mods: SimpleNamespace,
) -> None:
    uid = mods.identity.create_user(
        username="alice", password="pw-123456789",
    )
    gid = mods.store.ensure_group("oidc:eng")
    mods.store.ensure_group("local-team")  # non-oidc, must not appear
    mods.store.add_group_member(gid, member_user_id=uid)

    memberships = mods.store.user_group_memberships_by_name_prefix(uid, "oidc:")
    assert memberships == {"oidc:eng": gid}

    assert mods.store.remove_group_member(gid, uid) is True
    assert mods.store.remove_group_member(gid, uid) is False
    assert mods.store.user_group_memberships_by_name_prefix(uid, "oidc:") == {}


# ── Invariant 4: one connection factory, both modes ─────────────────


def test_single_connection_factory_serves_both_modes(
    mods: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three former connection helpers collapse to one: every path
    routes through ``identity.open_connection``."""
    identity = mods.identity
    real = identity.open_connection
    calls: list[int] = []

    def spy() -> sqlite3.Connection:
        calls.append(1)
        return real()

    monkeypatch.setattr(identity, "open_connection", spy)

    # (a) raw / transactional mode — store.connect
    raw = mods.store.connect()
    assert isinstance(raw, sqlite3.Connection)
    assert raw.row_factory is sqlite3.Row
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    raw.close()

    # (b) admin handler helper routes through the SAME store factory
    c2 = mods.admin_users_api._connect()
    assert isinstance(c2, sqlite3.Connection)
    c2.close()

    # (c) autocommit self-open mode — store.connection
    with mods.store.connection() as c3:
        c3.execute("SELECT 1")

    # (d) identity._connect ctx-mgr also builds on the factory
    with identity._connect() as c4:
        c4.execute("SELECT 1")

    assert len(calls) == 4  # all four modes → one factory
