"""Migration 0021 — scheduled_directive table.

Asserts the ``scheduled_directive`` store lands with the columns, types,
NOT-NULL constraints, defaults, and the ``idx_scheduled_directive_due``
index the wait-loop firing mechanism depends on. Mirrors the canonical
migration-test pattern (``test_migration_0013_agent_role.py``): bootstrap
a fresh DB via ``init_database()`` + the Alembic chain, then reflect the
schema with ``PRAGMA`` at the raw sqlite layer.
"""

from __future__ import annotations

import os
import sqlite3

import agent_mcp


def _run_alembic_upgrade(project_dir: str) -> None:
    from alembic import command
    from alembic.config import Config

    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(pkg_root, "migrations")
    )
    os.environ["MCP_PROJECT_DIR"] = project_dir
    command.upgrade(cfg, "head")


def _bootstrap_fresh_db(tmp_path) -> str:
    project_dir = str(tmp_path)
    (tmp_path / ".agent").mkdir()
    db_path = str(tmp_path / ".agent" / "mcp_state.db")
    os.environ["MCP_PROJECT_DIR"] = project_dir
    from agent_mcp.db import engine as _engine

    _engine.reset_engine_cache()
    from agent_mcp.db.schema import init_database

    init_database()
    _run_alembic_upgrade(project_dir)
    _engine.reset_engine_cache()
    return db_path


def test_scheduled_directive_table_and_columns(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "scheduled_directive" in tables

        cols = {
            r[1]: {"type": r[2], "notnull": r[3], "default": r[4], "pk": r[5]}
            for r in conn.execute(
                "PRAGMA table_info(scheduled_directive)"
            ).fetchall()
        }
        expected = {
            "directive_id",
            "agent_id",
            "prompt",
            "interval_seconds",
            "next_due_at",
            "enabled",
            "status",
            "until_at",
            "max_runs",
            "run_count",
            "created_at",
            "created_by",
            "updated_at",
            "updated_by",
        }
        assert expected <= set(cols), (
            f"missing columns: {expected - set(cols)}"
        )

        assert cols["directive_id"]["pk"] == 1
        for nn in (
            "agent_id",
            "prompt",
            "interval_seconds",
            "next_due_at",
            "enabled",
            "status",
            "run_count",
            "created_at",
        ):
            assert cols[nn]["notnull"] == 1, f"{nn} should be NOT NULL"
        for nullable in ("until_at", "max_runs", "updated_at", "updated_by"):
            assert cols[nullable]["notnull"] == 0, (
                f"{nullable} should be nullable"
            )
        assert str(cols["enabled"]["default"]) == "1"
        assert str(cols["run_count"]["default"]) == "0"
        assert cols["status"]["default"] in ("'active'", '"active"')
    finally:
        conn.close()


def test_scheduled_directive_due_index(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        indexes = {
            r[1]
            for r in conn.execute(
                "PRAGMA index_list(scheduled_directive)"
            ).fetchall()
        }
        assert "idx_scheduled_directive_due" in indexes
        idx_cols = [
            r[2]
            for r in conn.execute(
                "PRAGMA index_info(idx_scheduled_directive_due)"
            ).fetchall()
        ]
        assert idx_cols == ["agent_id", "enabled", "next_due_at"]
    finally:
        conn.close()
