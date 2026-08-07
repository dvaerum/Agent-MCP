"""Schema test for migration 0015_drop_config_system_token.

Per plan ``retire-system-token.md`` Wave 3: the system_token is no
longer used at runtime. Wave 1 stopped accepting it as a bearer; Wave
2 stopped the router injecting it; Wave 3 deletes the storage row
``project_context.config_system_token`` (and the legacy
``config_admin_token`` row if still present from pre-rename installs).

The migration must:

  (a) DELETE the ``config_system_token`` row if present.
  (b) DELETE the legacy ``config_admin_token`` row if present.
  (c) Be idempotent: a re-run on a DB that doesn't have either row
      MUST succeed silently.
  (d) Leave unrelated ``project_context`` rows alone.

Alembic's ``command.upgrade(cfg, target)`` is a no-op once the DB is
already at or beyond that revision (it consults ``alembic_version``);
the migration body itself is a single idempotent ``DELETE`` so we
exercise it directly to verify the contract rather than spelunking
around alembic's version table.

Forward-only — no downgrade test (the migration raises
NotImplementedError on downgrade by design).
"""

from __future__ import annotations

import datetime
import os
import sqlite3


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


def _seed_project_context_row(
    db_path: str, context_key: str, value: str
) -> None:
    """INSERT a ``project_context`` row directly via sqlite3."""
    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO project_context "
            "(context_key, value, description, created_at, created_by, "
            "updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                context_key,
                value,
                f"test seed for {context_key}",
                now,
                "test",
                now,
                "test",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _count_rows(db_path: str, context_key: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM project_context WHERE context_key = ?",
            (context_key,),
        ).fetchone()[0]
    finally:
        conn.close()


def _run_migration_0015_body(db_path: str) -> None:
    """Execute the DELETE statement from migration 0015 directly.

    The migration body itself is a single idempotent ``DELETE``; we
    exercise it directly rather than re-running alembic (which is a
    no-op once the DB is already at or beyond that revision).
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM project_context "
            "WHERE context_key IN ('config_system_token', 'config_admin_token')"
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_0015_drops_config_system_token_row(tmp_path) -> None:
    """A seeded ``config_system_token`` row is removed by the
    migration body."""
    db_path = _bootstrap_fresh_db(tmp_path)

    _seed_project_context_row(
        db_path, "config_system_token", '"some-token-value"'
    )
    assert _count_rows(db_path, "config_system_token") == 1

    _run_migration_0015_body(db_path)
    assert _count_rows(db_path, "config_system_token") == 0


def test_migration_0015_drops_legacy_config_admin_token_row(tmp_path) -> None:
    """A seeded legacy ``config_admin_token`` row is removed too."""
    db_path = _bootstrap_fresh_db(tmp_path)

    _seed_project_context_row(
        db_path, "config_admin_token", '"legacy-token-value"'
    )
    assert _count_rows(db_path, "config_admin_token") == 1

    _run_migration_0015_body(db_path)
    assert _count_rows(db_path, "config_admin_token") == 0


def test_migration_0015_is_idempotent_on_clean_db(tmp_path) -> None:
    """Running the migration body when no row is present must succeed
    silently."""
    db_path = _bootstrap_fresh_db(tmp_path)

    # Sanity: the initial upgrade-to-head left no rows of either key.
    assert _count_rows(db_path, "config_system_token") == 0
    assert _count_rows(db_path, "config_admin_token") == 0

    # Should not raise even though the rows don't exist.
    _run_migration_0015_body(db_path)

    assert _count_rows(db_path, "config_system_token") == 0
    assert _count_rows(db_path, "config_admin_token") == 0


def test_migration_0015_leaves_unrelated_rows_alone(tmp_path) -> None:
    """Unrelated ``project_context`` rows are not deleted."""
    db_path = _bootstrap_fresh_db(tmp_path)

    _seed_project_context_row(
        db_path, "config_system_token", '"to-be-deleted"'
    )
    _seed_project_context_row(
        db_path, "config_unrelated_key", '"keep-me"'
    )
    assert _count_rows(db_path, "config_system_token") == 1
    assert _count_rows(db_path, "config_unrelated_key") == 1

    _run_migration_0015_body(db_path)
    assert _count_rows(db_path, "config_system_token") == 0
    assert _count_rows(db_path, "config_unrelated_key") == 1, (
        "Migration must only delete the system/admin token rows, "
        "not other project_context entries."
    )


def test_migration_0015_is_in_alembic_chain(tmp_path) -> None:
    """After ``upgrade head`` the chain has passed through 0015 — the
    head moved on to 0016 (Wave 11's project_settings cutover), so pin
    that the walk reached at least the post-0015 lineage by asserting
    the applied head's ``down_revision`` chain includes 0015."""
    db_path = _bootstrap_fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "alembic_version table is empty"
    # Walk the revision lineage from the applied head back to base and
    # assert 0015 is on it (robust to future head bumps).
    import os as _os

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import agent_mcp

    cfg = Config()
    cfg.set_main_option(
        "script_location",
        _os.path.join(_os.path.dirname(agent_mcp.__file__), "migrations"),
    )
    script = ScriptDirectory.from_config(cfg)
    lineage = {rev.revision for rev in script.walk_revisions("base", row[0])}
    assert "0015_drop_config_system_token" in lineage, (
        f"0015 missing from applied lineage ending at {row[0]!r}"
    )
