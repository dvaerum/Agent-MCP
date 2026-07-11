"""Arch-deepening R2 #1b — unify membership inserts + passwordless-user.

#1a introduced the ``RouterStore`` connection-injection seam and moved the
forked group *reads* behind it. #1b closes the WRITE forks:

  * ``group_membership`` was INSERTed inline in four places
    (``group_resolver.add_group_member``, ``admin_users_api`` inside its
    ``BEGIN IMMEDIATE`` handler, and ``sso._add_user_to_group_idempotent``);
  * ``project_membership`` was INSERTed inline in four places
    (``identity.add_project_membership`` + the first-user loop,
    ``admin_users_api`` for the user- and group-grant shapes, and
    ``sso``'s first-user loop);
  * ``sso._create_passwordless_user`` was a near-verbatim copy of
    ``identity.create_user`` differing only in ``password_hash = NULL``.

These tests pin the two invariants the unification must preserve:

  1. a membership row written THROUGH the store is byte-identical to the
     old inline INSERT (same columns, same NULLs, same role default), and
     enlists in a caller's open transaction without opening a 2nd
     connection;
  2. ``create_user(password_hash=None)`` yields an SSO-shaped row
     (NULL ``password_hash``, stamped ``sso_subject``, and the same
     first-user sysadmin/membership bootstrap) — identical to what the
     deleted ``_create_passwordless_user`` produced.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest


# ── Fixtures (mirror tests/router/test_arch_r2_1_router_store.py) ────


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


def _group_rows(identity, group_id: str) -> list[dict]:
    with identity._connect() as conn:
        cur = conn.execute(
            "SELECT group_id, member_user_id, member_group_id "
            "FROM group_membership WHERE group_id = ?",
            (group_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def _project_rows(identity, project_name: str) -> list[dict]:
    with identity._connect() as conn:
        cur = conn.execute(
            "SELECT project_name, user_id, group_id, role "
            "FROM project_membership WHERE project_name = ?",
            (project_name,),
        )
        return [dict(r) for r in cur.fetchall()]


# ── group_membership writer ─────────────────────────────────────────


def test_store_add_group_member_user_edge_row_shape(store, identity) -> None:
    """A user→group edge written through the store matches the inline
    INSERT: (group_id, member_user_id, NULL member_group_id)."""
    _make_group(identity, "g_eng", "eng")
    uid = identity.create_user(username="alice", password="pw-123456789")
    store.add_group_member("g_eng", member_user_id=uid)
    rows = _group_rows(identity, "g_eng")
    assert rows == [
        {"group_id": "g_eng", "member_user_id": uid, "member_group_id": None}
    ]


def test_store_add_group_member_enlists_in_open_conn(
    store, identity, monkeypatch
) -> None:
    """Handed an open BEGIN IMMEDIATE connection, the store INSERTs on THAT
    connection and never self-opens a second one."""
    _make_group(identity, "g_eng", "eng")
    uid = identity.create_user(username="alice", password="pw-123456789")

    def _boom(*_a, **_k):
        raise AssertionError("store opened a second connection")

    monkeypatch.setattr(identity, "_connect", _boom)
    conn = sqlite3.connect(str(identity.get_router_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        store.add_group_member("g_eng", member_user_id=uid, conn=conn)
        # visible inside the same txn before commit
        seen = conn.execute(
            "SELECT member_user_id FROM group_membership WHERE group_id = ?",
            ("g_eng",),
        ).fetchone()
        assert seen["member_user_id"] == uid
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_store_add_group_member_rejects_cycle(store, identity) -> None:
    """group→group edges keep the cycle guard."""
    from agent_mcp.router.group_resolver import CycleDetected

    _make_group(identity, "g_a", "a")
    _make_group(identity, "g_b", "b")
    store.add_group_member("g_a", member_group_id="g_b")
    with pytest.raises(CycleDetected):
        store.add_group_member("g_b", member_group_id="g_a")


def test_store_add_group_member_requires_exactly_one_member(
    store, identity
) -> None:
    _make_group(identity, "g_eng", "eng")
    with pytest.raises(ValueError):
        store.add_group_member("g_eng")


# ── project_membership writer ───────────────────────────────────────


def test_store_add_project_membership_user_grant_idempotent(
    store, identity
) -> None:
    """The canonical (identity) shape: user grant, default role, OR IGNORE.
    Re-running is a no-op — one row, role defaults 'operator'."""
    uid = identity.create_user(username="alice", password="pw-123456789")
    store.add_project_membership("proj_a", user_id=uid, or_ignore=True)
    store.add_project_membership("proj_a", user_id=uid, or_ignore=True)
    assert _project_rows(identity, "proj_a") == [
        {
            "project_name": "proj_a",
            "user_id": uid,
            "group_id": None,
            "role": "operator",
        }
    ]


def test_store_add_project_membership_group_grant_with_role(
    store, identity
) -> None:
    """The admin group-grant shape: (project, NULL user_id, group_id, role)."""
    _make_group(identity, "g_eng", "eng")
    store.add_project_membership("proj_a", group_id="g_eng", role="viewer")
    assert _project_rows(identity, "proj_a") == [
        {
            "project_name": "proj_a",
            "user_id": None,
            "group_id": "g_eng",
            "role": "viewer",
        }
    ]


def test_store_add_project_membership_user_grant_raises_on_conflict(
    store, identity
) -> None:
    """The admin user-grant shape uses a plain INSERT: a duplicate raises
    IntegrityError (the handler maps it to a 409) rather than silently
    ignoring."""
    uid = identity.create_user(username="alice", password="pw-123456789")
    store.add_project_membership("proj_a", user_id=uid, role="operator")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_project_membership("proj_a", user_id=uid, role="operator")


def test_store_add_project_membership_enlists_in_open_conn(
    store, identity, monkeypatch
) -> None:
    uid = identity.create_user(username="alice", password="pw-123456789")

    def _boom(*_a, **_k):
        raise AssertionError("store opened a second connection")

    monkeypatch.setattr(identity, "_connect", _boom)
    conn = sqlite3.connect(str(identity.get_router_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        store.add_project_membership(
            "proj_a", user_id=uid, role="viewer", conn=conn
        )
        seen = conn.execute(
            "SELECT role FROM project_membership WHERE project_name = ?",
            ("proj_a",),
        ).fetchone()
        assert seen["role"] == "viewer"
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_store_add_project_membership_requires_exactly_one_grantee(
    store, identity
) -> None:
    with pytest.raises(ValueError):
        store.add_project_membership("proj_a")


# ── create_user(password_hash=None) replaces _create_passwordless_user ──


def _sso_row(identity, user_id: str) -> dict:
    with identity._connect() as conn:
        cur = conn.execute(
            "SELECT username, password_hash, is_sysadmin, sso_subject "
            "FROM users WHERE user_id = ?",
            (user_id,),
        )
        return dict(cur.fetchone())


def test_create_user_passwordless_shape(identity) -> None:
    """create_user(password_hash=None) mints a NULL-password, subject-stamped
    row — the SSO shape the deleted _create_passwordless_user produced."""
    uid = identity.create_user(
        username="ssoer",
        password=None,
        email="e@x.com",
        password_hash=None,
        sso_subject="oidc:iss:sub",
        is_sysadmin=False,
        bootstrap_sysadmin=False,
    )
    row = _sso_row(identity, uid)
    assert row["password_hash"] is None
    assert row["sso_subject"] == "oidc:iss:sub"
    assert row["username"] == "ssoer"


def test_create_user_passwordless_first_user_bootstrap_gate(identity) -> None:
    """On an empty table the first passwordless user gets sysadmin ONLY when
    bootstrap_sysadmin=True (matches _create_passwordless_user's gate)."""
    # bootstrap declined → not sysadmin
    uid1 = identity.create_user(
        username="first",
        password=None,
        password_hash=None,
        sso_subject="oidc:iss:a",
        bootstrap_sysadmin=False,
    )
    assert _sso_row(identity, uid1)["is_sysadmin"] == 0


def test_create_user_passwordless_first_user_bootstrap_opt_in(
    router_db, monkeypatch
) -> None:
    """With bootstrap_sysadmin=True the first passwordless user IS sysadmin."""
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()
    uid = identity.create_user(
        username="first",
        password=None,
        password_hash=None,
        sso_subject="oidc:iss:a",
        bootstrap_sysadmin=True,
    )
    assert _sso_row(identity, uid)["is_sysadmin"] == 1


def test_password_user_first_bootstrap_unchanged(identity) -> None:
    """The password path is unchanged: the first user is sysadmin by default
    (bootstrap_sysadmin defaults True for the password create path)."""
    uid = identity.create_user(username="op", password="pw-123456789")
    row = _sso_row(identity, uid)
    assert row["is_sysadmin"] == 1
    assert row["password_hash"] is not None
    assert row["sso_subject"] is None


def test_find_or_create_sso_user_uses_create_user(router_db) -> None:
    """Integration: the SSO JIT path produces a passwordless, subject-stamped
    row through the unified create_user."""
    import agent_mcp.router.identity as identity
    import agent_mcp.router.sso as sso

    importlib.reload(identity)
    importlib.reload(sso)
    identity.init_router_db()

    user = sso.find_or_create_sso_user(
        email="alice@x.com",
        preferred_username="alice",
        subject="oidc:iss:alice",
        email_verified=True,
    )
    row = _sso_row(identity, user["user_id"])
    assert row["password_hash"] is None
    assert row["sso_subject"] == "oidc:iss:alice"
    # _create_passwordless_user is gone.
    assert not hasattr(sso, "_create_passwordless_user")
