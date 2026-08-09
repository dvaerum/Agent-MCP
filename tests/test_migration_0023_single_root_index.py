"""R15-BL-1 structural backstop: migration 0023 adds a partial UNIQUE
index enforcing the single-root-task invariant at the DB layer.

The invariant (one task with ``parent_task IS NULL`` per project DB)
was previously only enforced (inconsistently) in application code. This
migration installs the backstop no app path can bypass:

    CREATE UNIQUE INDEX idx_tasks_single_root
        ON tasks ((parent_task IS NULL)) WHERE parent_task IS NULL

(The indexed expression is a constant ``1`` for every root row, so all
roots collide on one key; SQLite would otherwise treat each NULL
``parent_task`` as distinct and NOT enforce uniqueness.)

Pre-existing-violation strategy
-------------------------------
This bug already allowed multi-root DBs to exist, so a naive
``CREATE UNIQUE INDEX`` would hard-fail on real data. The migration
first REPAIRS: it keeps the OLDEST root (created_at ASC, task_id ASC —
the legitimate "first" root) and re-parents every other root under it
(no data loss, and consistent with the invariant that every non-first
task has a parent), then creates the index. These tests cover the
clean single-root apply AND the multi-root repair.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3

import pytest


def _load_migration():
    """Load the 0023 migration module by file path.

    Its filename starts with a digit, so it cannot be imported by dotted
    name; the repair helper (``_collapse_extra_roots``) is unit-tested by
    loading the file directly — the same module Alembic executes.
    """
    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    path = os.path.join(
        pkg_root, "migrations", "versions",
        "0023_single_root_task_index.py",
    )
    spec = importlib.util.spec_from_file_location("_mig_0023", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- bootstrap helpers -----------------------------------------------------


def _run_alembic_upgrade(project_dir: str, revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(pkg_root, "migrations")
    )
    db_path = os.path.join(project_dir, ".agent", "mcp_state.db")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    os.environ["MCP_PROJECT_DIR"] = project_dir
    command.upgrade(cfg, revision)


def _bootstrap_fresh_db(tmp_path) -> str:
    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    os.environ["MCP_PROJECT_DIR"] = project_dir

    from agent_mcp.db import engine as _engine

    _engine._engine = None  # type: ignore[attr-defined]

    from agent_mcp.db.schema import init_database

    init_database()
    return db_path


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    parent: str | None,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO tasks "
        "(task_id, title, created_by, status, priority, created_at, "
        "updated_at, parent_task) "
        "VALUES (?, ?, 'admin', 'unassigned', 'medium', ?, ?, ?)",
        (task_id, f"title-{task_id}", created_at, created_at, parent),
    )


def _index_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_tasks_single_root'"
    ).fetchone()
    return row is not None


def _root_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT task_id FROM tasks WHERE parent_task IS NULL "
            "ORDER BY task_id"
        ).fetchall()
    ]


# --- the repair helper (unit-level, like 0017's _sanitize_context_keys) ----


def test_collapse_extra_roots_reparents_under_oldest(tmp_path) -> None:
    """``_collapse_extra_roots`` keeps the oldest root and re-parents the
    rest under it, returning the (reparented_id, survivor_id) pairs."""
    mig = _load_migration()

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT, "
        "created_by TEXT, status TEXT, priority TEXT, created_at TEXT, "
        "updated_at TEXT, parent_task TEXT, child_tasks TEXT)"
    )
    # Three roots; t_old is the oldest → survivor.
    _insert_task(conn, "t_mid", None, "2026-01-02T00:00:00")
    _insert_task(conn, "t_old", None, "2026-01-01T00:00:00")
    _insert_task(conn, "t_new", None, "2026-01-03T00:00:00")
    # A legitimate child of t_old — must be untouched.
    _insert_task(conn, "c1", "t_old", "2026-01-04T00:00:00")
    conn.commit()

    renames = mig._collapse_extra_roots(conn)

    # Exactly one root remains, and it is the oldest.
    assert _root_ids(conn) == ["t_old"]
    # The two younger roots were re-parented under the survivor.
    assert {r for r, _ in renames} == {"t_mid", "t_new"}
    assert all(parent == "t_old" for _, parent in renames)
    for tid in ("t_mid", "t_new"):
        row = conn.execute(
            "SELECT parent_task FROM tasks WHERE task_id = ?", (tid,)
        ).fetchone()
        assert row[0] == "t_old"
    # The survivor's child_tasks mirror now lists all its children.
    import json as _json

    children = set(
        _json.loads(
            conn.execute(
                "SELECT child_tasks FROM tasks WHERE task_id='t_old'"
            ).fetchone()[0]
            or "[]"
        )
    )
    assert {"t_mid", "t_new"} <= children


def test_collapse_extra_roots_noop_on_single_root(tmp_path) -> None:
    """No repair when there is already at most one root."""
    mig = _load_migration()

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT, "
        "created_by TEXT, status TEXT, priority TEXT, created_at TEXT, "
        "updated_at TEXT, parent_task TEXT, child_tasks TEXT)"
    )
    _insert_task(conn, "t_old", None, "2026-01-01T00:00:00")
    _insert_task(conn, "c1", "t_old", "2026-01-04T00:00:00")
    conn.commit()

    assert mig._collapse_extra_roots(conn) == []
    assert _root_ids(conn) == ["t_old"]


# --- full alembic upgrade paths --------------------------------------------


def test_migration_creates_index_and_blocks_second_root(tmp_path) -> None:
    """After ``upgrade head`` on a fresh DB, the partial unique index
    exists and rejects a second root at the DB layer."""
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        assert _index_exists(conn), "single-root index was not created"

        _insert_task(conn, "root1", None, "2026-01-01T00:00:00")
        conn.commit()
        # A second root must be rejected by the index.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_task(conn, "root2", None, "2026-01-02T00:00:00")
            conn.commit()
        conn.rollback()
        # Children (parent set) are unaffected.
        _insert_task(conn, "child1", "root1", "2026-01-03T00:00:00")
        _insert_task(conn, "child2", "root1", "2026-01-04T00:00:00")
        conn.commit()
    finally:
        conn.close()


def test_migration_applies_cleanly_on_existing_single_root(tmp_path) -> None:
    """A DB that already has ONE root migrates cleanly (no repair, index
    created)."""
    db_path = _bootstrap_fresh_db(tmp_path)

    # Simulate a legacy pre-0023 DB: drop the index create_all made, seed
    # a single root, then run the migration.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_tasks_single_root")
        _insert_task(conn, "root1", None, "2026-01-01T00:00:00")
        conn.commit()
    finally:
        conn.close()

    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        assert _index_exists(conn)
        assert _root_ids(conn) == ["root1"]
    finally:
        conn.close()


def test_migration_repairs_preexisting_multi_root(tmp_path) -> None:
    """The migration must NOT hard-fail on a DB that already violates the
    invariant: it repairs (re-parents extra roots under the oldest) then
    creates the index."""
    db_path = _bootstrap_fresh_db(tmp_path)

    # Legacy multi-root state: drop the fresh-DB index, insert 3 roots.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_tasks_single_root")
        _insert_task(conn, "r_mid", None, "2026-01-02T00:00:00")
        _insert_task(conn, "r_old", None, "2026-01-01T00:00:00")
        _insert_task(conn, "r_new", None, "2026-01-03T00:00:00")
        conn.commit()
    finally:
        conn.close()

    # Must apply without raising.
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        assert _index_exists(conn)
        # Exactly one root survives — the oldest.
        assert _root_ids(conn) == ["r_old"]
        # The extras were re-parented under it (no data lost).
        for tid in ("r_mid", "r_new"):
            row = conn.execute(
                "SELECT parent_task FROM tasks WHERE task_id = ?", (tid,)
            ).fetchone()
            assert row[0] == "r_old"
        # And the index now actively blocks a new second root.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_task(conn, "r_extra", None, "2026-01-05T00:00:00")
            conn.commit()
    finally:
        conn.close()
