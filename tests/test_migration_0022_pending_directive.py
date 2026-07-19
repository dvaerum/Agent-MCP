"""Migration 0022 — pending_directive (poke queue) table."""

from __future__ import annotations

import os
import sqlite3

import agent_mcp


def _bootstrap_fresh_db(tmp_path) -> str:
    from alembic import command
    from alembic.config import Config

    project_dir = str(tmp_path)
    (tmp_path / ".agent").mkdir()
    db_path = str(tmp_path / ".agent" / "mcp_state.db")
    os.environ["MCP_PROJECT_DIR"] = project_dir
    from agent_mcp.db import engine as _engine

    _engine.reset_engine_cache()
    from agent_mcp.db.schema import init_database

    init_database()
    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(pkg_root, "migrations")
    )
    command.upgrade(cfg, "head")
    _engine.reset_engine_cache()
    return db_path


def test_pending_directive_table_and_columns(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pending_directive" in tables
        cols = {
            r[1]: {"notnull": r[3], "pk": r[5]}
            for r in conn.execute(
                "PRAGMA table_info(pending_directive)"
            ).fetchall()
        }
        expected = {
            "poke_id", "agent_id", "prompt", "priority",
            "created_at", "created_by", "delivered_at",
        }
        assert expected <= set(cols)
        assert cols["poke_id"]["pk"] == 1
        for nn in ("agent_id", "prompt", "priority", "created_at"):
            assert cols[nn]["notnull"] == 1
        assert cols["delivered_at"]["notnull"] == 0

        indexes = {
            r[1]
            for r in conn.execute(
                "PRAGMA index_list(pending_directive)"
            ).fetchall()
        }
        assert "idx_pending_directive_undelivered" in indexes
    finally:
        conn.close()
