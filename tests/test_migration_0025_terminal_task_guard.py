"""OBS-R12-2 migration 0025 — DB-level terminal-state guard triggers.

"Terminal-state carve-out miss" is a recurring bug class: a task write
path forgets that a completed/cancelled/failed task is a frozen sink,
because the invariant was enforced opt-in, per call-site, in Python
(``task_tools._TERMINAL_TASK_STATUSES`` / ``_is_status_transition_
allowed``). Migration 0025 installs a structural backstop one layer
below every Python call-site: a trigger on ``tasks`` plus two triggers
on the ``task_notes`` side table (round-13 class-sweep addition) that
refuse the write at the DB layer itself.

These tests are the RED/GREEN pair the fix requires: each "blocks"
test first proves the mutation SUCCEEDS on a DB at revision 0024 (the
pre-fix state — this is the exact bug, reproduced with a raw SQL
UPDATE that bypasses every Python-level guard), then proves the SAME
raw mutation is refused once migrated to head (0025 applied).
"""

from __future__ import annotations

import os
import sqlite3

import pytest

# --- bootstrap helpers (mirrors tests/test_migration_0023_single_root_index.py) --


def _bootstrap_fresh_db(tmp_path) -> str:
    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    os.environ["MCP_PROJECT_DIR"] = project_dir

    from agent_mcp.db import engine as _engine

    _engine.reset_engine_cache()

    from agent_mcp.db.schema import init_database

    init_database()
    return db_path


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


def _run_alembic_downgrade(project_dir: str, revision: str) -> None:
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
    command.downgrade(cfg, revision)


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "pending",
    assigned_to: str | None = None,
    parent_task: str | None = None,
    title: str | None = None,
    notes: str = "[]",
) -> None:
    now = "2026-01-01T00:00:00"
    conn.execute(
        "INSERT INTO tasks (task_id, title, created_by, status, priority, "
        "created_at, updated_at, assigned_to, parent_task, child_tasks, "
        "depends_on_tasks, notes) "
        "VALUES (?, ?, 'admin', ?, 'medium', ?, ?, ?, ?, '[]', '[]', ?)",
        (
            task_id, title or f"title-{task_id}", status, now, now,
            assigned_to, parent_task, notes,
        ),
    )


def _insert_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    now = "2026-01-01T00:00:00"
    conn.execute(
        "INSERT INTO agents (token, agent_id, created_at, status, "
        "working_directory) VALUES (?, ?, ?, 'active', '/tmp')",
        (f"tok-{agent_id}", agent_id, now),
    )


def _trigger_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


_TASKS_TRIGGER = "trg_tasks_terminal_state_guard"
_NOTES_INSERT_TRIGGER = "trg_task_notes_terminal_guard_insert"
_NOTES_UPDATE_TRIGGER = "trg_task_notes_terminal_guard_update"


def test_upgrade_head_creates_all_three_triggers(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        assert _trigger_exists(conn, _TASKS_TRIGGER)
        assert _trigger_exists(conn, _NOTES_INSERT_TRIGGER)
        assert _trigger_exists(conn, _NOTES_UPDATE_TRIGGER)
    finally:
        conn.close()


# --- tasks table: RED (pre-fix) / GREEN (post-fix) ------------------------


def test_tasks_reassign_on_terminal_task_red_then_green(tmp_path) -> None:
    """RED: on a pre-0025 DB, a raw ``UPDATE tasks SET assigned_to=...``
    on a completed task SUCCEEDS — this is the exact bug (a write path
    that never checks ``_TERMINAL_TASK_STATUSES``). GREEN: the SAME
    raw UPDATE is refused once migrated to head."""
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))
    _run_alembic_downgrade(str(tmp_path), "0024_drop_config_aoe_settings")

    conn = sqlite3.connect(db_path)
    try:
        _insert_agent(conn, "worker-a")
        _insert_task(conn, "t1", status="completed")
        conn.commit()

        # RED: pre-fix, the DB itself does not refuse this.
        conn.execute(
            "UPDATE tasks SET assigned_to = ? WHERE task_id = ?",
            ("worker-a", "t1"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id='t1'"
        ).fetchone()
        assert row[0] == "worker-a", (
            "RED precondition failed: pre-0025, a reassign on a "
            "terminal task should succeed (that's the bug)"
        )
    finally:
        conn.close()

    # Now apply 0025.
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET assigned_to = ? WHERE task_id = ?",
                ("worker-b", "t1"),
            )
        # Row unchanged — the DB itself refused the write.
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id='t1'"
        ).fetchone()
        assert row[0] == "worker-a"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("status", "in_progress"),
        ("status", "cancelled"),  # terminal -> terminal is ALSO a sink
        ("priority", "high"),
        ("notes", '[{"x": 1}]'),
        ("title", "renamed"),
        ("description", "changed"),
    ],
)
def test_tasks_trigger_blocks_protected_field_on_terminal_task(
    tmp_path, field, new_value,
) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        _insert_task(conn, "t2", status="completed")
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"UPDATE tasks SET {field} = ? WHERE task_id = ?",
                (new_value, "t2"),
            )
    finally:
        conn.close()


def test_tasks_trigger_allows_clearing_assigned_to_on_terminal_task(
    tmp_path,
) -> None:
    """BL-R17-2 legitimate carve-out: agent-purge nulls a terminal
    task's ``assigned_to`` FK before hard-deleting the agent row,
    WITHOUT changing status (no resurrection). This must keep working."""
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        _insert_agent(conn, "worker-c")
        _insert_task(conn, "t3", status="completed", assigned_to="worker-c")
        conn.commit()

        conn.execute(
            "UPDATE tasks SET assigned_to = NULL WHERE task_id = ?",
            ("t3",),
        )
        conn.commit()
        row = conn.execute(
            "SELECT assigned_to, status FROM tasks WHERE task_id='t3'"
        ).fetchone()
        assert row == (None, "completed")
    finally:
        conn.close()


def test_tasks_trigger_allows_child_tasks_and_depends_on_writes_on_terminal_task(
    tmp_path,
) -> None:
    """Delete-cascade reference cleanup (``child_tasks`` on a terminal
    parent, ``depends_on_tasks`` on a terminal dependent) must keep
    working — these are NOT protected fields."""
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        _insert_task(conn, "t4", status="completed")
        conn.commit()

        conn.execute(
            "UPDATE tasks SET child_tasks = ?, depends_on_tasks = ? "
            "WHERE task_id = ?",
            ('["c1"]', '["d1"]', "t4"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT child_tasks, depends_on_tasks FROM tasks WHERE task_id='t4'"
        ).fetchone()
        assert row == ('["c1"]', '["d1"]')
    finally:
        conn.close()


def test_tasks_trigger_allows_writes_on_nonterminal_task(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        _insert_task(conn, "t5", status="pending")
        conn.commit()

        conn.execute(
            "UPDATE tasks SET status='completed', notes='[1]', "
            "priority='high', title='x', description='y', assigned_to='w' "
            "WHERE task_id = ?",
            ("t5",),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM tasks WHERE task_id='t5'"
        ).fetchone()
        assert row[0] == "completed"
    finally:
        conn.close()


# --- task_notes side table (round-13 class-sweep addition) ----------------


def test_task_notes_insert_red_then_green_on_terminal_parent(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))
    _run_alembic_downgrade(str(tmp_path), "0024_drop_config_aoe_settings")

    conn = sqlite3.connect(db_path)
    try:
        _insert_task(conn, "n1", status="completed")
        conn.commit()

        # RED: pre-fix, add_task_note's side table has no guard at all.
        conn.execute(
            "INSERT INTO task_notes (task_id, author, timestamp, text) "
            "VALUES (?, 'alice', '2026-01-01T00:00:00', 'sneaky')",
            ("n1",),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM task_notes WHERE task_id='n1'"
        ).fetchone()[0]
        assert count == 1, "RED precondition failed: pre-0025 insert should succeed"
    finally:
        conn.close()

    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_notes (task_id, author, timestamp, text) "
                "VALUES (?, 'alice', '2026-01-01T00:00:01', 'sneaky2')",
                ("n1",),
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM task_notes WHERE task_id='n1'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_task_notes_insert_allowed_on_nonterminal_parent(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        _insert_task(conn, "n2", status="pending")
        conn.commit()
        conn.execute(
            "INSERT INTO task_notes (task_id, author, timestamp, text) "
            "VALUES (?, 'alice', '2026-01-01T00:00:00', 'fine')",
            ("n2",),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM task_notes WHERE task_id='n2'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_task_notes_insert_allowed_when_parent_missing(tmp_path) -> None:
    """A note whose ``task_id`` matches no row in ``tasks`` is NOT
    blocked (NULL IN (...) is NULL, not true) — that's a job for the
    tool-layer NotFound check, not this trigger."""
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO task_notes (task_id, author, timestamp, text) "
            "VALUES ('does-not-exist', 'alice', '2026-01-01T00:00:00', 'x')"
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM task_notes WHERE task_id='does-not-exist'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_task_notes_update_blocked_on_terminal_parent(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        # Note added while the task is still non-terminal (the INSERT
        # trigger would otherwise refuse this note itself); the task is
        # driven terminal AFTERWARDS to isolate the UPDATE trigger.
        _insert_task(conn, "n3", status="pending")
        conn.execute(
            "INSERT INTO task_notes (note_id, task_id, author, timestamp, text) "
            "VALUES (1, 'n3', 'alice', '2026-01-01T00:00:00', 'orig')"
        )
        conn.execute("UPDATE tasks SET status = 'failed' WHERE task_id = 'n3'")
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE task_notes SET text = 'edited' WHERE note_id = 1"
            )
        text = conn.execute(
            "SELECT text FROM task_notes WHERE note_id=1"
        ).fetchone()[0]
        assert text == "orig"
    finally:
        conn.close()


def test_task_notes_delete_not_guarded_on_terminal_parent(tmp_path) -> None:
    """DELETE is deliberately unguarded (leaves room for a future
    cascade-delete-of-task cleanup fix)."""
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))

    conn = sqlite3.connect(db_path)
    try:
        _insert_task(conn, "n4", status="pending")
        conn.execute(
            "INSERT INTO task_notes (note_id, task_id, author, timestamp, text) "
            "VALUES (2, 'n4', 'alice', '2026-01-01T00:00:00', 'orig')"
        )
        conn.execute("UPDATE tasks SET status = 'cancelled' WHERE task_id = 'n4'")
        conn.commit()

        conn.execute("DELETE FROM task_notes WHERE note_id = 2")
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM task_notes WHERE note_id=2"
        ).fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_downgrade_drops_all_three_triggers(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    _run_alembic_upgrade(str(tmp_path))
    _run_alembic_downgrade(str(tmp_path), "0024_drop_config_aoe_settings")

    conn = sqlite3.connect(db_path)
    try:
        assert not _trigger_exists(conn, _TASKS_TRIGGER)
        assert not _trigger_exists(conn, _NOTES_INSERT_TRIGGER)
        assert not _trigger_exists(conn, _NOTES_UPDATE_TRIGGER)
    finally:
        conn.close()
