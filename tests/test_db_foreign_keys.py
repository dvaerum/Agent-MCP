"""Test suite for PR-2 of the database review improvements (item 4).

The 2026-06-02 database review flagged that `PRAGMA foreign_keys=ON`
is set per connection but **no FK constraints are declared in any
CREATE TABLE** — so the pragma is a no-op. This PR ships **four of
the seven** implicit FKs via Alembic migration 0007.

The other three (agent_messages.sender_id, agent_messages.recipient_id,
mcp_sessions.agent_id) were initially deferred — production data
showed all their orphans had agent_id='admin', the admin pseudo-agent
identity that lives in `g.admin_token` but had no row in the agents
table. PR-G1 (migration 0008) seeds that row and ships the deferred
FKs; coverage for those moved to `test_db_admin_pseudo_agent.py`.

Tests cover:
- All 4 FK constraints visible via `PRAGMA foreign_key_list(<table>)`
  after lifespan startup.
- Insert that violates a shipped FK is rejected when `foreign_keys=ON`.
- Pre-existing orphans on nullable FK columns are NULL-cleaned by the
  migration.
- The cleanup can be bypassed via
  `AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP=1` env var (safety hatch for
  operators who want to inspect orphans first); follow-on FK
  creation then fails loudly so the operator notices.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness import mcp_session


# Default to asyncio for the async tests; the two synchronous
# orphan-cleanup tests at the bottom override via their own decorators.
pytestmark = pytest.mark.asyncio


# (table, column, referenced_table, referenced_column) — the four
# FKs this migration ships (subset of the seven in the review; see
# module docstring for why the other three are deferred).
_REQUIRED_FKS = [
    ("agents", "current_task", "tasks", "task_id"),
    ("tasks", "parent_task", "tasks", "task_id"),
    ("tasks", "assigned_to", "agents", "agent_id"),
    ("claude_code_sessions", "agent_id", "agents", "agent_id"),
]

# The three FKs explicitly deferred to a follow-up PR — proven not
# present here so the deferral remains documented.
_DEFERRED_FKS = [
    ("agent_messages", "sender_id", "agents", "agent_id"),
    ("agent_messages", "recipient_id", "agents", "agent_id"),
    ("mcp_sessions", "agent_id", "agents", "agent_id"),
]


def _fk_list(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Return PRAGMA foreign_key_list(table) rows as (col, ref_table, ref_col)."""
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    # PRAGMA foreign_key_list columns:
    #   id, seq, table, from, to, on_update, on_delete, match
    return [(r[3], r[2], r[4]) for r in rows]


async def test_shipped_fk_constraints_declared(tmp_path) -> None:
    """Each shipped FK must be present in DDL after lifespan startup."""
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            for table, col, ref_table, ref_col in _REQUIRED_FKS:
                fks = _fk_list(conn, table)
                assert (col, ref_table, ref_col) in fks, (
                    f"missing FK {table}.{col} -> {ref_table}.{ref_col}; "
                    f"have {fks}"
                )
        finally:
            conn.close()


async def test_previously_deferred_fks_now_shipped(tmp_path) -> None:
    """The three admin-implicated FKs are now declared (PR-G1, migration 0008).

    Originally PR-2 (0007) deferred these three FKs because the
    application treated `admin` as a pseudo-agent with no row in
    `agents`. PR-G1 (0008) seeded that row and shipped the deferred
    FKs. This test guards against accidental regression that would
    re-remove them.

    Detailed coverage (FK violation rejection + admin-row seeding +
    `PRAGMA foreign_key_check` clean) lives in
    `test_db_admin_pseudo_agent.py`.
    """
    from agent_mcp.core.config import get_db_path

    async with mcp_session(tmp_path):
        conn = sqlite3.connect(str(get_db_path()))
        try:
            for table, col, ref_table, ref_col in _DEFERRED_FKS:
                fks = _fk_list(conn, table)
                assert (col, ref_table, ref_col) in fks, (
                    f"deferred FK {table}.{col} -> {ref_table}.{ref_col} "
                    f"should now be present after PR-G1; have {fks}"
                )
        finally:
            conn.close()


async def test_fk_violation_is_rejected(tmp_path) -> None:
    """With `foreign_keys=ON`, an orphan insert must fail.

    Uses `claude_code_sessions.agent_id` because the brand-new DB has
    zero rows in `agents`, so any agent_id we invent is an orphan and
    the FK should reject the insert.
    """
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path):
        conn = get_db_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO claude_code_sessions "
                    "(session_id, pid, parent_pid, first_detected, "
                    " last_activity, agent_id) "
                    "VALUES ('s', 100, 1, 't', 't', 'no-such-agent')"
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Orphan-cleanup behavior of the migration itself
# ---------------------------------------------------------------------------


def _seed_pre_fk_schema(db_path: Path) -> None:
    """Create the pre-PR-2 schema (no FKs) and seed orphan data.

    We run a copy of the relevant CREATE TABLE statements from the
    PR-1-era schema so the migration runs against the legacy DDL that
    real production DBs upgrade from.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE agents ("
            "  token TEXT PRIMARY KEY,"
            "  agent_id TEXT UNIQUE NOT NULL,"
            "  capabilities TEXT,"
            "  created_at TEXT NOT NULL,"
            "  status TEXT NOT NULL,"
            "  current_task TEXT,"
            "  working_directory TEXT NOT NULL,"
            "  color TEXT,"
            "  terminated_at TEXT,"
            "  updated_at TEXT,"
            "  aoe_session_id TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE tasks ("
            "  task_id TEXT PRIMARY KEY,"
            "  title TEXT NOT NULL,"
            "  description TEXT,"
            "  assigned_to TEXT,"
            "  created_by TEXT NOT NULL,"
            "  status TEXT NOT NULL,"
            "  priority TEXT NOT NULL,"
            "  created_at TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL,"
            "  parent_task TEXT,"
            "  child_tasks TEXT,"
            "  depends_on_tasks TEXT,"
            "  notes TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE agent_messages ("
            "  message_id TEXT PRIMARY KEY,"
            "  sender_id TEXT NOT NULL,"
            "  recipient_id TEXT NOT NULL,"
            "  message_content TEXT NOT NULL,"
            "  message_type TEXT NOT NULL DEFAULT 'text',"
            "  priority TEXT NOT NULL DEFAULT 'normal',"
            "  timestamp TEXT NOT NULL,"
            "  delivered BOOLEAN NOT NULL DEFAULT 0,"
            "  read BOOLEAN NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute(
            "CREATE TABLE claude_code_sessions ("
            "  session_id TEXT PRIMARY KEY,"
            "  pid INTEGER NOT NULL,"
            "  parent_pid INTEGER NOT NULL,"
            "  first_detected TEXT NOT NULL,"
            "  last_activity TEXT NOT NULL,"
            "  working_directory TEXT,"
            "  agent_id TEXT,"
            "  status TEXT DEFAULT 'detected',"
            "  git_commits TEXT,"
            "  metadata TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE mcp_sessions ("
            "  session_id TEXT PRIMARY KEY NOT NULL,"
            "  agent_id TEXT NOT NULL,"
            "  opened_at TEXT NOT NULL,"
            "  last_seen_at TEXT NOT NULL,"
            "  bearer_token_hash TEXT NOT NULL,"
            "  alias_used TEXT"
            ")"
        )
        # project_context is required for earlier migrations (0002)
        # that run as part of `upgrade head`. Seed with the post-0002
        # shape directly so the migration is a no-op for it.
        conn.execute(
            "CREATE TABLE project_context ("
            "  context_key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL,"
            "  description TEXT,"
            "  created_at TEXT,"
            "  created_by TEXT,"
            "  updated_at TEXT NOT NULL,"
            "  updated_by TEXT NOT NULL"
            ")"
        )
        # Stamp alembic_version so prior migrations are skipped and
        # only PR-2's 0007 runs. We pin to 0006 (the PR-1 head); the
        # PR-2 migration is the only one that should execute.
        conn.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) "
            "PRIMARY KEY NOT NULL)"
        )
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES "
            "('0006_db_review_indexes')"
        )
        # Seed one real agent + tasks.
        conn.execute(
            "INSERT INTO agents "
            "(token, agent_id, created_at, status, working_directory) "
            "VALUES ('tok-alice', 'alice', 't', 'active', '/tmp')"
        )
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, title, created_by, status, priority, created_at, "
            " updated_at) "
            "VALUES ('task-real', 'real', 'admin', 'pending', 'low', "
            " 't', 't')"
        )
        # Now seed orphans for every FK column:
        # 1. agents.current_task pointing nowhere
        conn.execute(
            "INSERT INTO agents "
            "(token, agent_id, created_at, status, working_directory, "
            " current_task) "
            "VALUES ('tok-orph', 'orph', 't', 'active', '/tmp', "
            " 'nonexistent-task')"
        )
        # 2. tasks.parent_task pointing nowhere
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, title, created_by, status, priority, created_at, "
            " updated_at, parent_task) "
            "VALUES ('task-orph-parent', 't', 'admin', 'pending', 'low', "
            " 't', 't', 'nonexistent-parent')"
        )
        # 3. tasks.assigned_to pointing to no agent
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, title, assigned_to, created_by, status, priority, "
            " created_at, updated_at) "
            "VALUES ('task-orph-assign', 't', 'nonexistent-agent', "
            " 'admin', 'pending', 'low', 't', 't')"
        )
        # 4 & 5. agent_messages with bogus sender / recipient
        conn.execute(
            "INSERT INTO agent_messages "
            "(message_id, sender_id, recipient_id, message_content, "
            " timestamp) "
            "VALUES ('m-orph-snd', 'bogus-sender', 'alice', 'x', 't')"
        )
        conn.execute(
            "INSERT INTO agent_messages "
            "(message_id, sender_id, recipient_id, message_content, "
            " timestamp) "
            "VALUES ('m-orph-rcp', 'alice', 'bogus-recipient', 'x', 't')"
        )
        # 6. claude_code_sessions with bogus agent_id
        conn.execute(
            "INSERT INTO claude_code_sessions "
            "(session_id, pid, parent_pid, first_detected, last_activity, "
            " agent_id) "
            "VALUES ('ccs-orph', 100, 1, 't', 't', 'bogus-agent')"
        )
        # 7. mcp_sessions with bogus agent_id
        conn.execute(
            "INSERT INTO mcp_sessions "
            "(session_id, agent_id, opened_at, last_seen_at, "
            " bearer_token_hash) "
            "VALUES ('mcps-orph', 'bogus-agent', 't', 't', 'h')"
        )
        conn.commit()
    finally:
        conn.close()


def _run_migration_to_head(db_path: Path, env: dict[str, str]) -> tuple[int, str, str]:
    """Run `alembic upgrade head` against db_path with the given env."""
    # Set MCP_PROJECT_DIR pointing at the parent so env.py resolves to
    # the same file.
    project_dir = db_path.parent.parent
    full_env = dict(os.environ)
    full_env.update(env)
    full_env["MCP_PROJECT_DIR"] = str(project_dir)
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(repo_root),
        env=full_env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


async def test_migration_cleans_up_orphans_by_default(tmp_path) -> None:
    """Without the bypass env var, the migration deletes orphan rows.

    Smoke-tested via an out-of-process `alembic upgrade head` against
    a hand-seeded legacy schema. The migration must:
      * Find every orphan column.
      * DELETE / NULL-out the offending rows so the FK constraint can
        be added.
      * Add the constraint (verified via PRAGMA foreign_key_list).
    """
    project_dir = tmp_path / "orphan-test-proj"
    (project_dir / ".agent").mkdir(parents=True)
    db_path = project_dir / ".agent" / "mcp_state.db"
    _seed_pre_fk_schema(db_path)

    code, out, err = _run_migration_to_head(db_path, env={})
    assert code == 0, f"alembic upgrade failed: stderr={err}\nstdout={out}"

    # FK constraints in place
    conn = sqlite3.connect(str(db_path))
    try:
        # All FKs declared
        for table, col, ref_table, ref_col in _REQUIRED_FKS:
            fks = _fk_list(conn, table)
            assert (col, ref_table, ref_col) in fks, (
                f"missing FK {table}.{col} -> {ref_table}.{ref_col} after migration"
            )

        # Real, non-orphan rows still present
        assert conn.execute(
            "SELECT 1 FROM agents WHERE agent_id='alice'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM tasks WHERE task_id='task-real'"
        ).fetchone()

        # Orphans on the 4 shipped FKs: all four columns are nullable,
        # so the migration NULLs the dangling pointer and keeps the
        # row. The deferred FKs' orphan rows (agent_messages,
        # mcp_sessions with bogus admin-ish agent_ids) are intentionally
        # left untouched — they'll be addressed in the follow-up that
        # seeds the admin pseudo-agent.
        orph_agent = conn.execute(
            "SELECT current_task FROM agents WHERE agent_id='orph'"
        ).fetchone()
        assert orph_agent is not None, "orphan agent row should be kept"
        assert orph_agent[0] is None, "agents.current_task orphan should be NULLed"

        orph_parent = conn.execute(
            "SELECT parent_task FROM tasks WHERE task_id='task-orph-parent'"
        ).fetchone()
        assert orph_parent is not None, "orphan-parent task row should be kept"
        assert orph_parent[0] is None, "tasks.parent_task orphan should be NULLed"

        orph_assign = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id='task-orph-assign'"
        ).fetchone()
        assert orph_assign is not None
        assert orph_assign[0] is None

        ccs = conn.execute(
            "SELECT agent_id FROM claude_code_sessions WHERE session_id='ccs-orph'"
        ).fetchone()
        assert ccs is not None
        assert ccs[0] is None

        # Previously-deferred FK orphan rows are now DELETEd by
        # migration 0008 (PR-G1). The agent_messages and mcp_sessions
        # orphans seeded above reference 'bogus-sender' /
        # 'bogus-recipient' / 'bogus-agent' — none of those are
        # 'admin', so they don't survive the NOT NULL FK cleanup.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM agent_messages "
                "WHERE message_id IN ('m-orph-snd', 'm-orph-rcp')"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM mcp_sessions WHERE session_id='mcps-orph'"
            ).fetchone()[0]
            == 0
        )
        # The synthetic admin row is seeded by migration 0008.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM agents WHERE agent_id='admin'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


async def test_migration_bypass_orphan_cleanup_fails_loudly(tmp_path) -> None:
    """With the bypass env var set, orphans aren't cleaned up.

    SQLite's batch_alter_table copies rows through a temp table; with
    `foreign_keys=ON` (the connection default in agent-mcp) and
    orphans still present, the copy raises FOREIGN KEY constraint
    failed. We assert the failure surfaces as a non-zero exit so an
    operator notices.
    """
    project_dir = tmp_path / "bypass-test-proj"
    (project_dir / ".agent").mkdir(parents=True)
    db_path = project_dir / ".agent" / "mcp_state.db"
    _seed_pre_fk_schema(db_path)

    code, out, err = _run_migration_to_head(
        db_path, env={"AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP": "1"}
    )
    assert code != 0, (
        f"expected non-zero exit when bypass set + orphans present; "
        f"got 0\nstdout={out}\nstderr={err}"
    )
