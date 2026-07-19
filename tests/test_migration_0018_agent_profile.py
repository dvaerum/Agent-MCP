"""Schema test for migration 0018_agent_profile_columns (agent self-service
profiles, 2026-07-19).

Per plan `agent-profile-self-service.md` §5, this migration adds four
nullable columns to the ``agents`` table:

  * ``profile``             TEXT NULL — self-authored prose
  * ``profile_updated_at``  TEXT NULL — ISO-8601; bumped only on content change
  * ``profile_reviewed_at`` TEXT NULL — ISO-8601; bumped on every review
  * ``profile_updated_by``  TEXT NULL — agent_id of last content editor

All four are nullable so every existing row stays valid without a
backfill. The columns exist on the ORM model too, so a fresh DB picks
them up via ``create_all()`` first and the migration's ADD COLUMN
branch is idempotent (the legacy upgrade path is the one that runs the
ALTER TABLE).

Mirrors ``test_migration_0013_agent_role.py`` — a column-add migration
against a freshly bootstrapped DB.
"""

from __future__ import annotations

import os
import sqlite3


_PROFILE_COLUMNS = (
    "profile",
    "profile_updated_at",
    "profile_reviewed_at",
    "profile_updated_by",
)


def _run_alembic_upgrade(project_dir: str) -> None:
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
    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    os.environ["MCP_PROJECT_DIR"] = project_dir

    from agent_mcp.db import engine as _engine
    _engine._engine = None  # type: ignore[attr-defined]

    from agent_mcp.db.schema import init_database

    init_database()
    _run_alembic_upgrade(project_dir)
    return db_path


def test_migration_0018_adds_four_profile_columns_nullable(tmp_path) -> None:
    """All four profile columns are present, TEXT, and nullable."""
    db_path = _bootstrap_fresh_db(tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        cols = {
            r[1]: {"type": r[2], "notnull": r[3], "default": r[4]}
            for r in conn.execute("PRAGMA table_info(agents)").fetchall()
        }
        for name in _PROFILE_COLUMNS:
            assert name in cols, f"migration did not add {name!r} to agents"
            info = cols[name]
            assert info["type"].upper() == "TEXT", (
                f"{name} expected TEXT, got {info['type']!r}"
            )
            assert info["notnull"] == 0, f"{name} must be nullable"
    finally:
        conn.close()


def test_migration_0018_existing_rows_get_null_profile(tmp_path) -> None:
    """A row inserted without the profile columns gets NULL for each."""
    db_path = _bootstrap_fresh_db(tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO agents "
            "(token, agent_id, capabilities, created_at, status, "
            "current_task, working_directory, color, terminated_at, "
            "updated_at, aoe_session_id, auto_event_loop, "
            "last_event_seen_at, agent_role) "
            "VALUES ('tok-w1', 'worker-1', '[]', "
            "'2026-07-19T10:00:00', 'created', NULL, '/tmp/w1', "
            "NULL, NULL, NULL, NULL, 1, NULL, 'worker')"
        )
        conn.commit()

        row = conn.execute(
            "SELECT profile, profile_updated_at, profile_reviewed_at, "
            "profile_updated_by FROM agents WHERE token = 'tok-w1'"
        ).fetchone()
        assert row == (None, None, None, None)
    finally:
        conn.close()


def test_migration_0018_accepts_profile_values(tmp_path) -> None:
    """A row may carry all four profile columns populated."""
    db_path = _bootstrap_fresh_db(tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO agents "
            "(token, agent_id, capabilities, created_at, status, "
            "current_task, working_directory, color, terminated_at, "
            "updated_at, aoe_session_id, auto_event_loop, "
            "last_event_seen_at, agent_role, profile, profile_updated_at, "
            "profile_reviewed_at, profile_updated_by) "
            "VALUES ('tok-m1', 'manager-1', '[]', "
            "'2026-07-19T10:00:00', 'created', NULL, '/tmp/m1', "
            "NULL, NULL, NULL, NULL, 1, NULL, 'manager', "
            "'I coordinate the team.', '2026-07-19T10:00:00', "
            "'2026-07-19T10:00:00', NULL)"
        )
        conn.commit()

        row = conn.execute(
            "SELECT profile, profile_updated_by FROM agents "
            "WHERE token = 'tok-m1'"
        ).fetchone()
        assert row[0] == "I coordinate the team."
        assert row[1] is None
    finally:
        conn.close()
