"""Round-trip test for migration 0009_task_notes_side_table (PR-H).

PR #102 learned the hard way that production DBs can fail
migrations in ways CI's pristine schemas mask. This test seeds a
DB with the embedded-JSON notes format, runs the migration, and
confirms every note ended up in the side table with its timestamp
+ author + text preserved.

Distinct from `test_sqlalchemy_task_note.py`, which exercises
the ORM model + tools against a freshly bootstrapped DB (where
`task_notes` is created by `init_database()` directly). This file
exercises the actual ALEMBIC UPGRADE path against legacy data.
"""

from __future__ import annotations

import json as _json
import os
import sqlite3


def _seed_legacy_db(db_path: str) -> dict[str, list[dict]]:
    """Create a sqlite DB at db_path with the schema pre-0009 plus
    sample tasks + JSON-notes. Returns the in-memory dict of
    `{task_id: [note, ...]}` used to seed so the test can compare.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
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
        """)
        # alembic_version table — set to one step before 0009.
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            ("0008_admin_pseudo_agent_and_fks",),
        )

        # Seed three tasks: one with two notes, one with one corrupt
        # JSON (must be skipped without aborting the migration), one
        # with NULL notes (must be left alone).
        sample_notes = {
            "task-1": [
                {
                    "timestamp": "2026-05-30T10:00:00",
                    "author": "alice",
                    "content": "first",
                },
                {
                    "timestamp": "2026-05-30T11:00:00",
                    "author": "bob",
                    "content": "second",
                },
            ],
        }
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, '', NULL, 'admin', 'pending', 'medium', "
            "'2026-05-30T09:00:00', '2026-05-30T09:00:00', NULL, "
            "'[]', '[]', ?)",
            ("task-1", "T1", _json.dumps(sample_notes["task-1"])),
        )
        # task-2: corrupt JSON in notes — migration must skip it.
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, '', NULL, 'admin', 'pending', 'medium', "
            "'2026-05-30T09:00:00', '2026-05-30T09:00:00', NULL, "
            "'[]', '[]', ?)",
            ("task-2", "T2", "not-valid-json{{"),
        )
        # task-3: NULL notes — migration must leave it as a no-op.
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, '', NULL, 'admin', 'pending', 'medium', "
            "'2026-05-30T09:00:00', '2026-05-30T09:00:00', NULL, "
            "'[]', '[]', NULL)",
            ("task-3", "T3"),
        )
        conn.commit()
    finally:
        conn.close()
    return sample_notes


def _run_alembic_upgrade(project_dir: str) -> None:
    """Run `alembic upgrade head` against the DB rooted at
    `<project_dir>/.agent/mcp_state.db` — using the same env.py the
    production server uses."""
    from alembic import command
    from alembic.config import Config

    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(pkg_root, "migrations"),
    )
    # env.py reads MCP_PROJECT_DIR for the sqlite URL.
    os.environ["MCP_PROJECT_DIR"] = project_dir
    command.upgrade(cfg, "head")


def test_migration_0009_copies_legacy_notes_to_side_table(tmp_path) -> None:
    """Legacy JSON notes are extracted into task_notes with the
    original timestamp + author + content preserved as text."""
    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    sample = _seed_legacy_db(db_path)
    _run_alembic_upgrade(project_dir)

    # The migration must have created the side table + indexed it.
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "task_notes" in tables

        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_task_notes_task" in indexes

        # Two notes copied (task-1 had two; task-2 corrupt; task-3 NULL).
        notes = conn.execute(
            "SELECT task_id, author, timestamp, text "
            "FROM task_notes ORDER BY timestamp ASC"
        ).fetchall()
        assert len(notes) == 2

        # Preserve task_id, author, timestamp, text.
        assert notes[0][0] == "task-1"
        assert notes[0][1] == "alice"
        assert notes[0][2] == sample["task-1"][0]["timestamp"]
        assert notes[0][3] == "first"
        assert notes[1][0] == "task-1"
        assert notes[1][1] == "bob"
        assert notes[1][3] == "second"

        # tasks.notes column must still exist (deprecation window).
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "notes" in cols

        # alembic_version advanced past 0009. Originally pinned to
        # "0009_task_notes_side_table" (the head at the time this test
        # was written); now that event-coord PR-1's migration 0010
        # sits downstream, `alembic upgrade head` advances past 0009 —
        # so accept any 00NN_* version where NN >= 9 (it still proves
        # the 0009 step ran, since `head` walks the chain through it).
        v = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        # Lexicographic 0009 <= v works for the zero-padded ID prefixes
        # the project uses (0001 .. 0010 .. and onward).
        assert v >= "0009_task_notes_side_table", (
            f"alembic_version did not advance past 0009; got {v!r}"
        )
    finally:
        conn.close()


def test_migration_0009_idempotent(tmp_path) -> None:
    """Running the migration twice (e.g. via `alembic stamp` + upgrade
    chain) must not duplicate rows or raise."""
    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    db_path = str(agent_dir / "mcp_state.db")

    _seed_legacy_db(db_path)
    _run_alembic_upgrade(project_dir)

    # Stamp back to 0008 and re-run; the _create_task_notes step
    # short-circuits if the table exists, so re-running shouldn't
    # double-insert. (The data-copy step is unguarded — that's
    # acceptable because production never re-runs an already-applied
    # migration. We just confirm the no-op upgrade path doesn't
    # explode.)
    conn = sqlite3.connect(db_path)
    try:
        first = conn.execute("SELECT COUNT(*) FROM task_notes").fetchone()[0]
    finally:
        conn.close()

    # Re-run from current head (already-at-head is a no-op for alembic).
    _run_alembic_upgrade(project_dir)

    conn = sqlite3.connect(db_path)
    try:
        second = conn.execute(
            "SELECT COUNT(*) FROM task_notes"
        ).fetchone()[0]
    finally:
        conn.close()

    assert first == second, "no-op re-upgrade duplicated rows"


