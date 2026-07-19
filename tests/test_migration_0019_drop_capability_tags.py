"""Migration-path test for 0019_drop_capability_tags (retire structured
capability-tag routing, 2026-07-19).

Per plan `agent-profile-self-service.md` §6/§7 (PR5), this migration
physically drops two columns via Alembic ``batch_alter_table``:

  * ``agents.capabilities``
  * ``tasks.required_capabilities``

This is the feature's highest-risk migration (a table rebuild on the two
busiest tables), so it gets a dedicated migration-path test: bootstrap a
pre-0019 DB shape that STILL carries both columns, seed a real agent row
(carrying ``capabilities``) and task row (carrying
``required_capabilities``), run ``alembic upgrade head``, and assert:

  (a) both columns are gone from ``PRAGMA table_info``;
  (b) every OTHER column + row value survives byte-identical;
  (c) the ``ck_agents_agent_role_domain`` CHECK on ``agents`` survives
      (an out-of-domain ``agent_role`` insert is still rejected);
  (d) the tasks hot-path indexes survive.

The pre-0019 shape is reconstructed on top of a fresh ORM DB (whose
models no longer declare the columns) by re-adding the two columns via
raw ``ALTER TABLE ... ADD COLUMN``, then stamping alembic at 0018 so the
subsequent ``upgrade head`` runs ONLY 0019 against a column-present DB —
exactly the legacy-DB path this migration exists for.

Mirrors ``test_migration_0018_agent_profile.py`` /
``test_migration_0013_agent_role.py`` for the fresh-DB bootstrap.
"""

from __future__ import annotations

import os
import sqlite3

import pytest


def _alembic_config():
    from alembic.config import Config

    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(pkg_root, "migrations"),
    )
    return cfg


def _bootstrap_pre_0019_db(tmp_path) -> str:
    """Fresh ORM DB + re-added retired columns, stamped at 0018.

    ``init_database()`` lands the post-drop ORM shape (the models no
    longer declare ``capabilities`` / ``required_capabilities``). We then
    re-add both columns via raw ALTER so the DB looks like a legacy
    pre-0019 database, seed rows carrying tag values, and stamp alembic at
    0018 so the caller's ``upgrade head`` runs only 0019.
    """
    from alembic import command

    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    os.environ["MCP_PROJECT_DIR"] = project_dir

    from agent_mcp.db import engine as _engine
    _engine._engine = None  # type: ignore[attr-defined]

    from agent_mcp.db.schema import init_database

    init_database()

    # Re-add the retired columns to simulate a real pre-0019 DB.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN capabilities TEXT")
        conn.execute("ALTER TABLE tasks ADD COLUMN required_capabilities TEXT")
        conn.commit()
    finally:
        conn.close()

    # Stamp at 0018 so `upgrade head` runs only the 0019 drop.
    cfg = _alembic_config()
    command.stamp(cfg, "0018_agent_profile_columns")
    return db_path


def _seed_rows(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO agents "
            "(token, agent_id, capabilities, created_at, status, "
            "current_task, working_directory, color, terminated_at, "
            "updated_at, aoe_session_id, auto_event_loop, "
            "last_event_seen_at, agent_role, profile, profile_updated_at, "
            "profile_reviewed_at, profile_updated_by) "
            "VALUES ('tok-w1', 'worker-1', '[\"python\", \"docker\"]', "
            "'2026-07-19T10:00:00', 'active', 'task_deadbeef0001', "
            "'/tmp/w1', '#abcdef', NULL, '2026-07-19T10:05:00', 'aoe1', 1, "
            "'2026-07-19T10:06:00', 'worker', 'I build things.', "
            "'2026-07-19T10:00:00', '2026-07-19T10:00:00', NULL)"
        )
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, title, description, assigned_to, created_by, status, "
            "priority, created_at, updated_at, parent_task, child_tasks, "
            "depends_on_tasks, notes, required_capabilities) "
            "VALUES ('task_deadbeef0001', 'Build the widget', 'do it', "
            "'worker-1', 'admin', 'in_progress', 'high', "
            "'2026-07-19T09:00:00', '2026-07-19T09:30:00', NULL, '[]', "
            "'[]', '[]', '[\"python\"]')"
        )
        conn.commit()
    finally:
        conn.close()


def _run_upgrade_head(project_dir: str) -> None:
    from alembic import command

    os.environ["MCP_PROJECT_DIR"] = project_dir
    cfg = _alembic_config()
    command.upgrade(cfg, "head")


def test_migration_0019_drops_both_columns(tmp_path) -> None:
    """After the upgrade both retired columns are gone."""
    db_path = _bootstrap_pre_0019_db(tmp_path)
    _seed_rows(db_path)

    conn = sqlite3.connect(db_path)
    try:
        agent_cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "capabilities" in agent_cols
        assert "required_capabilities" in task_cols
    finally:
        conn.close()

    _run_upgrade_head(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        agent_cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "capabilities" not in agent_cols, (
            "0019 must drop agents.capabilities"
        )
        assert "required_capabilities" not in task_cols, (
            "0019 must drop tasks.required_capabilities"
        )
    finally:
        conn.close()


def test_migration_0019_preserves_all_other_columns_and_rows(tmp_path) -> None:
    """Every surviving column keeps its byte-identical value."""
    db_path = _bootstrap_pre_0019_db(tmp_path)
    _seed_rows(db_path)
    _run_upgrade_head(str(tmp_path))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        agent = conn.execute(
            "SELECT * FROM agents WHERE token = 'tok-w1'"
        ).fetchone()
        assert agent is not None
        assert agent["agent_id"] == "worker-1"
        assert agent["status"] == "active"
        assert agent["current_task"] == "task_deadbeef0001"
        assert agent["working_directory"] == "/tmp/w1"
        assert agent["color"] == "#abcdef"
        assert agent["updated_at"] == "2026-07-19T10:05:00"
        assert agent["aoe_session_id"] == "aoe1"
        assert agent["auto_event_loop"] == 1
        assert agent["last_event_seen_at"] == "2026-07-19T10:06:00"
        assert agent["agent_role"] == "worker"
        assert agent["profile"] == "I build things."
        assert agent["profile_reviewed_at"] == "2026-07-19T10:00:00"

        task = conn.execute(
            "SELECT * FROM tasks WHERE task_id = 'task_deadbeef0001'"
        ).fetchone()
        assert task is not None
        assert task["title"] == "Build the widget"
        assert task["description"] == "do it"
        assert task["assigned_to"] == "worker-1"
        assert task["created_by"] == "admin"
        assert task["status"] == "in_progress"
        assert task["priority"] == "high"
        assert task["parent_task"] is None
        assert task["notes"] == "[]"
    finally:
        conn.close()


def test_migration_0019_preserves_agent_role_check_constraint(tmp_path) -> None:
    """The ck_agents_agent_role_domain CHECK survives the rebuild."""
    db_path = _bootstrap_pre_0019_db(tmp_path)
    _seed_rows(db_path)
    _run_upgrade_head(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agents "
                "(token, agent_id, created_at, status, working_directory, "
                "auto_event_loop, agent_role) "
                "VALUES ('tok-x', 'bogus', '2026-07-19T10:00:00', 'created', "
                "'/tmp/x', 1, 'invalid')"
            )
            conn.commit()
    finally:
        conn.close()


def test_migration_0019_preserves_task_indexes(tmp_path) -> None:
    """The tasks hot-path indexes survive the rebuild."""
    db_path = _bootstrap_pre_0019_db(tmp_path)
    _seed_rows(db_path)
    _run_upgrade_head(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        idx = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='tasks'"
            )
        }
        assert "idx_tasks_status" in idx
        assert "idx_tasks_priority" in idx
        assert "idx_tasks_assigned_to_updated_at" in idx
        # The batch rebuild must NOT drop the DESC ordering on the
        # wait_for_events hot-path composite index (0019 restores it).
        assert "DESC" in idx["idx_tasks_assigned_to_updated_at"].upper(), (
            "0019 must preserve the DESC ordering on "
            "idx_tasks_assigned_to_updated_at after the tasks rebuild"
        )
    finally:
        conn.close()
