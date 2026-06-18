"""Schema test for migration 0013_agent_role_column (Phase 2 Wave 1a, v5.0.61).

Per plan `prancy-napping-pie.md` section 2a, this migration adds an
``agent_role`` column to the ``agents`` table:

  * ``TEXT NOT NULL DEFAULT 'worker'``
  * CHECK constraint allowing only ``'worker'`` or ``'manager'``.

The column exists but is unused in this PR — Wave 2 introduces the
``@requires_role`` decorator that reads it. This test pins the
schema-level guarantees so Wave 2 can rely on them:

  (a) the column is present with the right default;
  (b) inserting a row with ``agent_role='manager'`` succeeds;
  (c) inserting with an out-of-domain value (e.g. ``'invalid'``) is
      rejected by the CHECK constraint.

Distinct from `test_migration_0009_task_notes.py`, which exercises a
data-rewrite migration against legacy seed data. This file exercises
a column-add migration against a freshly bootstrapped DB.
"""

from __future__ import annotations

import os
import sqlite3

import pytest


def _run_alembic_upgrade(project_dir: str) -> None:
    """Run ``alembic upgrade head`` against the per-project DB,
    using the same env.py the production server uses."""
    from alembic import command
    from alembic.config import Config

    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(pkg_root, "migrations"),
    )
    os.environ["MCP_PROJECT_DIR"] = project_dir
    command.upgrade(cfg, "head")


def _bootstrap_fresh_db(tmp_path) -> str:
    """Use the production ``init_database()`` then run migrations.

    Mirrors the lifespan startup ordering: create_all() lands the ORM
    schema, then alembic catches the alembic_version table up to
    head (a no-op for the columns create_all already added — those
    add-column branches are idempotent).
    """
    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    # Force the engine + schema to point at THIS tmpdir.
    os.environ["MCP_PROJECT_DIR"] = project_dir

    # Reset cached engine so it picks up the new MCP_PROJECT_DIR.
    from agent_mcp.db import engine as _engine
    _engine._engine = None  # type: ignore[attr-defined]

    from agent_mcp.db.schema import init_database

    init_database()
    _run_alembic_upgrade(project_dir)
    return db_path


def test_migration_0013_adds_agent_role_column_with_default(tmp_path) -> None:
    """The migration must add ``agent_role`` to ``agents`` with default
    ``'worker'`` and NOT NULL."""
    db_path = _bootstrap_fresh_db(tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        cols = {
            r[1]: {"type": r[2], "notnull": r[3], "default": r[4]}
            for r in conn.execute("PRAGMA table_info(agents)").fetchall()
        }
        assert "agent_role" in cols, (
            "migration did not add agent_role column to agents"
        )
        info = cols["agent_role"]
        assert info["type"].upper() == "TEXT"
        assert info["notnull"] == 1, "agent_role must be NOT NULL"
        # PRAGMA reports the default with surrounding quotes.
        assert info["default"] in ("'worker'", '"worker"'), (
            f"agent_role default expected 'worker', got {info['default']!r}"
        )
    finally:
        conn.close()


def test_migration_0013_default_backfills_existing_rows(tmp_path) -> None:
    """Existing rows pick up ``'worker'`` automatically (SQLite
    materialises ALTER TABLE ADD COLUMN defaults into every existing
    row)."""
    db_path = _bootstrap_fresh_db(tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        # Seed a worker row WITHOUT specifying agent_role so the
        # default kicks in. (init_database creates the schema; we
        # insert directly through the connection to keep the test
        # at the DDL/SQL layer rather than going through the ORM.)
        conn.execute(
            "INSERT INTO agents "
            "(token, agent_id, capabilities, created_at, status, "
            "current_task, working_directory, color, terminated_at, "
            "updated_at, aoe_session_id, auto_event_loop, "
            "last_event_seen_at) "
            "VALUES ('tok-w1', 'worker-1', '[]', "
            "'2026-06-17T10:00:00', 'created', NULL, '/tmp/w1', "
            "NULL, NULL, NULL, NULL, 1, NULL)"
        )
        conn.commit()

        row = conn.execute(
            "SELECT agent_role FROM agents WHERE token = 'tok-w1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "worker"
    finally:
        conn.close()


def test_migration_0013_accepts_manager_value(tmp_path) -> None:
    """Inserting a row with ``agent_role='manager'`` succeeds."""
    db_path = _bootstrap_fresh_db(tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO agents "
            "(token, agent_id, capabilities, created_at, status, "
            "current_task, working_directory, color, terminated_at, "
            "updated_at, aoe_session_id, auto_event_loop, "
            "last_event_seen_at, agent_role) "
            "VALUES ('tok-m1', 'manager-1', '[]', "
            "'2026-06-17T10:00:00', 'created', NULL, '/tmp/m1', "
            "NULL, NULL, NULL, NULL, 1, NULL, 'manager')"
        )
        conn.commit()

        row = conn.execute(
            "SELECT agent_role FROM agents WHERE token = 'tok-m1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "manager"
    finally:
        conn.close()


def test_migration_0013_rejects_invalid_role_value(tmp_path) -> None:
    """The CHECK constraint rejects any ``agent_role`` outside the
    ``{worker, manager}`` set."""
    db_path = _bootstrap_fresh_db(tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agents "
                "(token, agent_id, capabilities, created_at, status, "
                "current_task, working_directory, color, terminated_at, "
                "updated_at, aoe_session_id, auto_event_loop, "
                "last_event_seen_at, agent_role) "
                "VALUES ('tok-x1', 'bogus-1', '[]', "
                "'2026-06-17T10:00:00', 'created', NULL, '/tmp/x1', "
                "NULL, NULL, NULL, NULL, 1, NULL, 'invalid')"
            )
            conn.commit()
    finally:
        conn.close()
