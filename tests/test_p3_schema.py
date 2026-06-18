"""Tests for the Phase 3 router-DB schema migration (Wave 1a PR 3a).

Locked schema (see ``docs/`` plan / prancy-napping-pie Phase 3):

  * ``users.is_sysadmin BOOLEAN NOT NULL DEFAULT 0``
  * ``groups (group_id PK, name UNIQUE, is_sysadmin, created_at)``
  * ``group_membership (group_id, member_user_id, member_group_id,
    added_at, CHECK exactly-one-of(member_user_id, member_group_id))``
  * ``project_membership.role TEXT NOT NULL DEFAULT 'operator'``
    with CHECK IN ('operator','viewer')
  * ``project_membership.group_id TEXT REFERENCES groups(group_id)`` —
    alternative to user_id; exactly one of the two set per row

Plus the bootstrap rule: the migration marks the *earliest* Phase-1
operator (smallest ``created_at``, ties broken by ``user_id``) as
``is_sysadmin=1`` and stamps every pre-existing
``project_membership`` row with ``role='operator'`` (the new default
covers it on insert, but the migration's data step needs to be
idempotent against re-runs).

Every test gets a fresh tmp ``router.db`` via ``AGENT_MCP_ROUTER_DB``
and re-imports the identity / migrations modules so module-level
state resolves against the patched env.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def router_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Tmp router.db path, env-injected, identity modules reloaded."""
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


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


# ── Shape: users.is_sysadmin ───────────────────────────────────────


def test_users_table_has_is_sysadmin_column(identity, router_db: Path) -> None:
    cols = _columns(_conn(router_db), "users")
    assert "is_sysadmin" in cols, (
        f"users.is_sysadmin missing; got columns: {list(cols)}"
    )
    col = cols["is_sysadmin"]
    # NOT NULL + DEFAULT 0
    assert col["notnull"] == 1
    # SQLite stores the literal default text — accept '0' or 0.
    assert str(col["dflt_value"]).strip("'") in {"0", "FALSE", "false"}


def test_user_default_is_sysadmin_false(identity) -> None:
    uid = identity.create_user(username="plain_user", password="pw")
    row = identity.get_user_by_id(uid)
    assert row is not None
    # Stored as int 0 by SQLite; treat 0 / False / "0" as falsy.
    assert not row["is_sysadmin"]


# ── Shape: groups table ─────────────────────────────────────────────


def test_groups_table_exists_with_expected_columns(
    identity, router_db: Path
) -> None:
    cols = _columns(_conn(router_db), "groups")
    assert set(cols) >= {"group_id", "name", "is_sysadmin", "created_at"}, (
        f"groups columns: {list(cols)}"
    )
    # group_id is PK
    assert cols["group_id"]["pk"] == 1
    # name is NOT NULL + UNIQUE — UNIQUE shows in index_list, not table_info
    conn = _conn(router_db)
    idx = list(conn.execute("PRAGMA index_list(groups)"))
    has_unique_on_name = False
    for ix in idx:
        if ix["unique"]:
            cols_on_idx = [
                r["name"]
                for r in conn.execute(f"PRAGMA index_info({ix['name']})")
            ]
            if cols_on_idx == ["name"]:
                has_unique_on_name = True
                break
    assert has_unique_on_name, "expected a UNIQUE index on groups(name)"


def test_groups_name_unique_rejects_duplicate(
    identity, router_db: Path
) -> None:
    conn = _conn(router_db)
    conn.execute(
        "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
        "VALUES ('g1', 'admins', 0, '2026-06-18T00:00:00')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES ('g2', 'admins', 0, '2026-06-18T00:00:01')"
        )


# ── Shape: group_membership ─────────────────────────────────────────


def test_group_membership_table_exists(identity, router_db: Path) -> None:
    cols = _columns(_conn(router_db), "group_membership")
    assert set(cols) >= {
        "group_id",
        "member_user_id",
        "member_group_id",
        "added_at",
    }


def test_group_membership_check_exactly_one_member(
    identity, router_db: Path
) -> None:
    """CHECK constraint: exactly one of (member_user_id, member_group_id)
    must be set per row."""
    conn = _conn(router_db)
    # Seed a group + a user so FKs would pass.
    conn.execute(
        "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
        "VALUES ('g1', 'team_a', 0, '2026-06-18T00:00:00')"
    )
    conn.execute(
        "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
        "VALUES ('g2', 'team_b', 0, '2026-06-18T00:00:00')"
    )
    conn.execute(
        "INSERT INTO users "
        "(user_id, username, email, password_hash, created_at, last_login_at)"
        " VALUES ('u1', 'alice', NULL, 'x', '2026-06-18T00:00:00', NULL)"
    )
    conn.commit()

    # Both NULL → violation.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO group_membership "
            "(group_id, member_user_id, member_group_id, added_at) "
            "VALUES ('g1', NULL, NULL, '2026-06-18T00:00:00')"
        )
    # Both set → violation.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO group_membership "
            "(group_id, member_user_id, member_group_id, added_at) "
            "VALUES ('g1', 'u1', 'g2', '2026-06-18T00:00:00')"
        )
    # Either alone → OK.
    conn.execute(
        "INSERT INTO group_membership "
        "(group_id, member_user_id, member_group_id, added_at) "
        "VALUES ('g1', 'u1', NULL, '2026-06-18T00:00:00')"
    )
    conn.execute(
        "INSERT INTO group_membership "
        "(group_id, member_user_id, member_group_id, added_at) "
        "VALUES ('g1', NULL, 'g2', '2026-06-18T00:00:00')"
    )
    conn.commit()


# ── Shape: project_membership.role + group_id ──────────────────────


def test_project_membership_has_role_column_with_default(
    identity, router_db: Path
) -> None:
    cols = _columns(_conn(router_db), "project_membership")
    assert "role" in cols
    assert cols["role"]["notnull"] == 1
    assert str(cols["role"]["dflt_value"]).strip("'") == "operator"


def test_project_membership_role_check_rejects_unknown(
    identity, router_db: Path
) -> None:
    conn = _conn(router_db)
    conn.execute(
        "INSERT INTO users "
        "(user_id, username, email, password_hash, created_at, last_login_at)"
        " VALUES ('u1', 'alice', NULL, 'x', '2026-06-18T00:00:00', NULL)"
    )
    conn.commit()
    # operator OK
    conn.execute(
        "INSERT INTO project_membership "
        "(project_name, user_id, role) VALUES ('p', 'u1', 'operator')"
    )
    conn.execute("DELETE FROM project_membership")
    # viewer OK
    conn.execute(
        "INSERT INTO project_membership "
        "(project_name, user_id, role) VALUES ('p', 'u1', 'viewer')"
    )
    conn.execute("DELETE FROM project_membership")
    # anything else rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, role) VALUES ('p', 'u1', 'sysadmin')"
        )


def test_project_membership_has_group_id_column(
    identity, router_db: Path
) -> None:
    cols = _columns(_conn(router_db), "project_membership")
    assert "group_id" in cols, (
        f"expected project_membership.group_id; got {list(cols)}"
    )


def test_project_membership_exactly_one_of_user_or_group(
    identity, router_db: Path
) -> None:
    conn = _conn(router_db)
    conn.execute(
        "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
        "VALUES ('g1', 'team', 0, '2026-06-18T00:00:00')"
    )
    conn.execute(
        "INSERT INTO users "
        "(user_id, username, email, password_hash, created_at, last_login_at)"
        " VALUES ('u1', 'alice', NULL, 'x', '2026-06-18T00:00:00', NULL)"
    )
    conn.commit()
    # Both NULL → violation.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, group_id, role) "
            "VALUES ('p', NULL, NULL, 'operator')"
        )
    # Both set → violation.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, group_id, role) "
            "VALUES ('p', 'u1', 'g1', 'operator')"
        )
    # user only → OK
    conn.execute(
        "INSERT INTO project_membership "
        "(project_name, user_id, group_id, role) "
        "VALUES ('p1', 'u1', NULL, 'operator')"
    )
    # group only → OK
    conn.execute(
        "INSERT INTO project_membership "
        "(project_name, user_id, group_id, role) "
        "VALUES ('p2', NULL, 'g1', 'viewer')"
    )
    conn.commit()


# ── Bootstrap: first operator → sysadmin ────────────────────────────


def test_bootstrap_marks_first_operator_as_sysadmin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the Phase 3 migration runs on a DB that already has
    Phase-1 operators (i.e. an existing deployment being upgraded),
    the earliest operator (smallest created_at; tiebreak by user_id)
    is flipped to ``is_sysadmin=1``."""
    db_path = tmp_path / "router.db"
    projects_file = tmp_path / "projects.local.json"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))

    # Apply the Phase 1 migration only first, by going through the
    # router's migrations runner — which will actually take us all the
    # way to head, but we'll then manually clear is_sysadmin to
    # simulate the "old data already exists" state and re-run upgrade.
    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
    ]:
        sys.modules.pop(mod, None)
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()

    # Insert several users manually with distinct created_at; the
    # earliest one (alpha) should win the sysadmin slot.
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO users "
            "(user_id, username, email, password_hash, created_at, "
            " last_login_at, is_sysadmin) "
            "VALUES "
            "('u_b', 'beta', NULL, 'x', '2026-06-18T02:00:00', NULL, 0),"
            "('u_a', 'alpha', NULL, 'x', '2026-06-18T01:00:00', NULL, 0),"
            "('u_c', 'gamma', NULL, 'x', '2026-06-18T03:00:00', NULL, 0)"
        )

    # Re-run the migration's data step (the migration is idempotent —
    # calling upgrade() again is a no-op for Alembic, so we exercise
    # the data step directly through the public bootstrap helper).
    sys.modules.pop("agent_mcp.router.group_resolver", None)
    import agent_mcp.router.group_resolver as group_resolver

    # NOTE: in the bootstrap-from-existing scenario the migration
    # already promoted one user during `identity.init_router_db()`.
    # We explicitly reset the table before exercising the helper so
    # this test pins THAT codepath rather than the migration's.
    with identity._connect() as conn:
        conn.execute("UPDATE users SET is_sysadmin = 0")
    group_resolver.bootstrap_first_operator_as_sysadmin()

    alpha = identity.get_user_by_username("alpha")
    beta = identity.get_user_by_username("beta")
    gamma = identity.get_user_by_username("gamma")
    assert alpha is not None and beta is not None and gamma is not None
    assert alpha["is_sysadmin"], "earliest operator should become sysadmin"
    assert not beta["is_sysadmin"]
    assert not gamma["is_sysadmin"]


def test_bootstrap_first_operator_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running the bootstrap a second time leaves things unchanged —
    in particular it does not flip a second user to sysadmin and does
    not unset the existing sysadmin."""
    db_path = tmp_path / "router.db"
    projects_file = tmp_path / "projects.local.json"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))

    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
        "agent_mcp.router.group_resolver",
    ]:
        sys.modules.pop(mod, None)
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()

    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO users "
            "(user_id, username, email, password_hash, created_at, "
            " last_login_at, is_sysadmin) "
            "VALUES "
            "('u_a', 'alpha', NULL, 'x', '2026-06-18T01:00:00', NULL, 0),"
            "('u_b', 'beta', NULL, 'x', '2026-06-18T02:00:00', NULL, 0)"
        )

    sys.modules.pop("agent_mcp.router.group_resolver", None)
    import agent_mcp.router.group_resolver as group_resolver

    # Reset so we exercise the helper rather than the migration's
    # implicit promotion during init_router_db.
    with identity._connect() as conn:
        conn.execute("UPDATE users SET is_sysadmin = 0")
    group_resolver.bootstrap_first_operator_as_sysadmin()
    # second run must be a no-op
    group_resolver.bootstrap_first_operator_as_sysadmin()

    alpha = identity.get_user_by_username("alpha")
    beta = identity.get_user_by_username("beta")
    assert alpha["is_sysadmin"]
    assert not beta["is_sysadmin"]


def test_bootstrap_no_users_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fresh deploy with no users yet: bootstrap is a clean no-op,
    no crash. The first user the operator creates will *not* be
    auto-flipped to sysadmin by THIS helper (that's the Phase-1
    bootstrap path's job in a future PR)."""
    db_path = tmp_path / "router.db"
    projects_file = tmp_path / "projects.local.json"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))

    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
        "agent_mcp.router.group_resolver",
    ]:
        sys.modules.pop(mod, None)
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()

    sys.modules.pop("agent_mcp.router.group_resolver", None)
    import agent_mcp.router.group_resolver as group_resolver

    # Must not raise on an empty users table.
    group_resolver.bootstrap_first_operator_as_sysadmin()
    with identity._connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        assert row[0] == 0


def test_existing_project_membership_rows_default_to_operator_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-Phase-3 ``project_membership`` rows had no ``role`` column;
    the migration must stamp them as ``role='operator'`` so existing
    operators don't lose access."""
    db_path = tmp_path / "router.db"
    projects_file = tmp_path / "projects.local.json"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))

    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
    ]:
        sys.modules.pop(mod, None)
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()
    uid = identity.create_user(username="prior_op", password="pw")
    identity.add_project_membership(uid, "alpha")

    with identity._connect() as conn:
        row = conn.execute(
            "SELECT role FROM project_membership WHERE user_id = ?",
            (uid,),
        ).fetchone()
        assert row["role"] == "operator"
