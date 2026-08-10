"""OBS5 migration 0024 — purge retired ``config_aoe_*`` rows from
``project_settings``.

The ``aoe_notify`` feature + its ``config_aoe_*`` settings were removed
(superseded by the ADR-0021 delivery bridge). Migration 0024 deletes any
leftover ``config_aoe_*`` rows from the settings store. These tests pin:

  * the purge deletes ``config_aoe_*`` rows ONLY (non-AoE config rows and
    knowledge-shaped keys survive);
  * it is idempotent (a re-run purges nothing);
  * it is table-absent safe (a DB with no ``project_settings`` table is a
    no-op, not a crash);
  * the full Alembic chain to head purges a pre-existing seeded row.

The purge helper is unit-tested by loading the migration file directly
(its filename starts with a digit so it can't be imported by dotted
name) — the same module Alembic executes.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3

import sqlalchemy as sa


def _load_migration():
    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    path = os.path.join(
        pkg_root, "migrations", "versions",
        "0024_drop_config_aoe_settings.py",
    )
    spec = importlib.util.spec_from_file_location("_mig_0024", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_settings_engine(rows: list[tuple[str, str]]):
    """An in-memory SQLite engine with a ``project_settings`` table seeded
    with ``(context_key, value)`` rows."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE project_settings ("
                "context_key TEXT PRIMARY KEY, value TEXT, description TEXT, "
                "created_at TEXT, created_by TEXT, updated_at TEXT, "
                "updated_by TEXT)"
            )
        )
        for key, value in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO project_settings "
                    "(context_key, value) VALUES (:k, :v)"
                ),
                {"k": key, "v": value},
            )
    return engine


def _keys(engine) -> set[str]:
    with engine.connect() as conn:
        return {
            r[0]
            for r in conn.execute(
                sa.text("SELECT context_key FROM project_settings")
            )
        }


# --- unit: the purge helper -------------------------------------------------


def test_purge_deletes_config_aoe_rows_only() -> None:
    mig = _load_migration()
    engine = _make_settings_engine([
        ("config_aoe_notify_enabled", "true"),
        ("config_aoe_base_url", '"http://aoe.test"'),
        ("config_aoe_bearer_token", '"secret"'),
        ("config_allow_worker_to_worker", "true"),
        ("config_message_retention_days", "7"),
    ])

    with engine.begin() as conn:
        deleted = mig._purge_config_aoe_settings(conn)

    assert deleted == 3
    assert _keys(engine) == {
        "config_allow_worker_to_worker",
        "config_message_retention_days",
    }


def test_purge_is_idempotent() -> None:
    mig = _load_migration()
    engine = _make_settings_engine([
        ("config_aoe_base_url", '"http://aoe.test"'),
        ("config_allow_worker_to_worker", "true"),
    ])

    with engine.begin() as conn:
        assert mig._purge_config_aoe_settings(conn) == 1
    # Second run finds nothing to purge.
    with engine.begin() as conn:
        assert mig._purge_config_aoe_settings(conn) == 0
    assert _keys(engine) == {"config_allow_worker_to_worker"}


def test_purge_table_absent_is_noop() -> None:
    mig = _load_migration()
    engine = sa.create_engine("sqlite://")  # no project_settings table
    with engine.begin() as conn:
        assert mig._purge_config_aoe_settings(conn) == 0


# --- full alembic chain -----------------------------------------------------


def _alembic_cfg(project_dir: str):
    from alembic.config import Config

    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(pkg_root, "migrations")
    )
    os.environ["MCP_PROJECT_DIR"] = project_dir
    return cfg


def test_full_chain_purges_preexisting_config_aoe_row(tmp_path) -> None:
    """A ``config_aoe_*`` row seeded before 0024 runs is gone after
    ``upgrade head``; a sibling non-AoE config row survives."""
    from alembic import command

    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    os.environ["MCP_PROJECT_DIR"] = project_dir
    from agent_mcp.db import engine as _engine

    _engine.reset_engine_cache()
    from agent_mcp.db.schema import init_database

    init_database()
    cfg = _alembic_cfg(project_dir)
    command.upgrade(cfg, "head")

    # Rewind past 0024 (its downgrade is a no-op), seed a stale AoE row +
    # a surviving non-AoE config row, then re-apply to head.
    command.downgrade(cfg, "0023_single_root_task_index")

    now = "2026-08-10T00:00:00"
    conn = sqlite3.connect(db_path)
    try:
        for key, value in (
            ("config_aoe_bearer_token", '"stale-secret"'),
            ("config_message_retention_days", "7"),
        ):
            conn.execute(
                "INSERT INTO project_settings "
                "(context_key, value, description, created_at, created_by, "
                "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, value, "seed", now, "seed", now, "seed"),
            )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db_path)
    try:
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT context_key FROM project_settings"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "config_aoe_bearer_token" not in rows, (
        "0024 must purge the stale config_aoe_* row"
    )
    assert "config_message_retention_days" in rows, (
        "0024 must leave non-AoE config rows untouched"
    )
