"""RED tests for PR-1 of the event-coord plan.

Covers the schema migration that adds the per-agent + per-task event
coordination columns plus the central normalization helper used at
write time. Per the plan:

  (i)   migration applies cleanly on a fresh DB AND on a DB with
        existing agents/tasks
  (ii)  ``tasks.required_capabilities`` column accepts NULL + JSON list
  (iii) ``normalize_capabilities`` strips + lowercases + dedupes and
        preserves order of first occurrence
  (iv)  ``agents.auto_event_loop`` defaults TRUE for existing rows
        post-migration
  (v)   ``assign_task_tool_impl`` with
        ``required_capabilities=["Backend", "DB"]`` stores
        ``["backend", "db"]``
  (vi)  agent-create with ``capabilities=["BACKEND"]`` stores
        ``["backend"]``
  (vii) ``project_context["config_auto_event_loop_global"]`` defaults
        to ``true`` when missing — the dashboard reads it and falls
        back to True; here we assert the dashboard config-key
        constant exists and the default is True.

The tests are deliberately schema/contract-only and live alongside
``test_migration_0009_task_notes.py`` (PR-H pattern) — no MCP
transport, no Starlette, just the migration runner + the tool impls
poked directly.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Dict

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_legacy_db(db_path: str) -> None:
    """Create a sqlite DB at ``db_path`` with the schema as of 0009
    plus one existing agent and one existing task. The migration must
    cope with both.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE agents (
                token TEXT PRIMARY KEY,
                agent_id TEXT UNIQUE NOT NULL,
                capabilities TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                current_task TEXT,
                working_directory TEXT NOT NULL,
                color TEXT,
                terminated_at TEXT,
                updated_at TEXT,
                aoe_session_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                assigned_to TEXT,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                parent_task TEXT,
                child_tasks TEXT,
                depends_on_tasks TEXT,
                notes TEXT
            )
            """
        )
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            ("0009_task_notes_side_table",),
        )

        # Seed one existing agent + one existing task so the migration
        # backfill path is exercised, not just the DDL.
        conn.execute(
            "INSERT INTO agents (token, agent_id, capabilities, created_at, "
            "status, working_directory) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "tok-legacy",
                "legacy-agent",
                "[]",
                "2026-06-04T00:00:00",
                "created",
                "/tmp",
            ),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-task",
                "Legacy",
                "",
                None,
                "admin",
                "unassigned",
                "medium",
                "2026-06-04T00:00:00",
                "2026-06-04T00:00:00",
                None,
                "[]",
                "[]",
                "[]",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _run_alembic_upgrade(project_dir: str) -> None:
    from alembic import command
    from alembic.config import Config

    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(pkg_root, "migrations"))
    os.environ["MCP_PROJECT_DIR"] = project_dir
    command.upgrade(cfg, "head")


def _table_columns(db_path: str, table: str) -> Dict[str, sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        out: Dict[str, sqlite3.Row] = {}
        for row in cur.fetchall():
            # cid, name, type, notnull, dflt_value, pk
            out[row[1]] = row
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (i) + (iv) — migration applies cleanly + backfills auto_event_loop=TRUE
# ---------------------------------------------------------------------------


def test_migration_applies_on_fresh_db_creates_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_globals: None,
) -> None:
    """Fresh DB path: init_database() bootstraps the raw-SQL schema,
    then `alembic upgrade head` plays every migration against it
    (the 0001 baseline / 0002 ownership cols / etc are all
    schema-aware idempotent). End state must have the three new
    columns."""
    project_dir = tmp_path / "fresh"
    project_dir.mkdir()
    monkeypatch.setenv("MCP_PROJECT_DIR", str(project_dir))

    from agent_mcp.db.schema import init_database
    from agent_mcp.db.migrations_runner import run_migrations_upgrade

    init_database()
    run_migrations_upgrade()

    db_path = str(project_dir / ".agent" / "mcp_state.db")
    agents_cols = _table_columns(db_path, "agents")
    tasks_cols = _table_columns(db_path, "tasks")

    assert "auto_event_loop" in agents_cols, (
        f"auto_event_loop missing; have: {sorted(agents_cols)}"
    )
    assert "last_event_seen_at" in agents_cols, (
        f"last_event_seen_at missing; have: {sorted(agents_cols)}"
    )
    # PR5 retired structured capability-tag routing: migration 0019 drops
    # tasks.required_capabilities (0010 added it, 0019 removes it), so the
    # end-of-chain shape must NOT carry the column.
    assert "required_capabilities" not in tasks_cols, (
        f"required_capabilities should be dropped by 0019; "
        f"have: {sorted(tasks_cols)}"
    )
    assert "capabilities" not in agents_cols, (
        f"agents.capabilities should be dropped by 0019; "
        f"have: {sorted(agents_cols)}"
    )

    # auto_event_loop is NOT NULL with default 1.
    info = agents_cols["auto_event_loop"]
    # PRAGMA columns: (cid, name, type, notnull, dflt_value, pk)
    assert info[3] == 1, (
        f"auto_event_loop must be NOT NULL (notnull=1), got {info[3]}"
    )
    # SQLite reports default as the literal it parsed; for INTEGER 1
    # this is "1".
    assert str(info[4]) == "1", (
        f"auto_event_loop default must be 1, got {info[4]!r}"
    )


def test_migration_backfills_auto_event_loop_true_for_existing_agents(
    tmp_path: Path,
) -> None:
    project_dir = str(tmp_path / "legacy")
    Path(project_dir).mkdir()
    agent_dir = Path(project_dir) / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    _seed_legacy_db(db_path)
    _run_alembic_upgrade(project_dir)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT auto_event_loop, last_event_seen_at "
            "FROM agents WHERE agent_id = ?",
            ("legacy-agent",),
        ).fetchone()
        assert row is not None, "legacy-agent row went missing post-migration"
        assert row[0] == 1, (
            "auto_event_loop must be TRUE (1) for legacy agents; "
            f"got {row[0]!r}"
        )
        assert row[1] is None, (
            "last_event_seen_at must default to NULL on backfill; "
            f"got {row[1]!r}"
        )

        # Migration also bumped alembic_version. The chain runs all
        # the way to head; for PR-1 we only care that 0010 has been
        # applied (i.e. version_num is >= 0010_*). Post-PR-W3 the head
        # is 0011_orm_is_source_of_truth (a no-op marker); compare by
        # numeric prefix rather than exact value so future no-op
        # markers don't drift this test again.
        ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        ver_prefix = ver.split("_", 1)[0]
        assert ver_prefix.isdigit() and int(ver_prefix) >= 10, (
            f"alembic_version should advance to a >=0010 migration; "
            f"got {ver!r}"
        )

        # foreign_key_check must be clean (no orphans introduced).
        fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_problems == [], (
            f"foreign_key_check returned issues: {fk_problems}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (vii) — config_auto_event_loop_global default semantics
# ---------------------------------------------------------------------------


def test_global_event_loop_config_key_defaults_to_true() -> None:
    """``config_auto_event_loop_global`` defaults to True when the row
    is missing (defaults are applied at read time; the migration must
    NOT pre-seed the row).

    ADR-0018: the default is owned by the backend registry
    (``agent_mcp.core.settings_schema``) — the single source of truth —
    not the frontend's former hardcoded POLICIES list. Assert against
    the registry via ``default_for``.
    """
    from agent_mcp.core.settings_schema import KNOWN_SETTING_KEYS, default_for

    assert "config_auto_event_loop_global" in KNOWN_SETTING_KEYS, (
        "the settings-schema registry must register the global "
        "event-loop key"
    )
    assert default_for("config_auto_event_loop_global") is True, (
        "default for config_auto_event_loop_global must be True "
        "(registry is the single source of truth)"
    )
