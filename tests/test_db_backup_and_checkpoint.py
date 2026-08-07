"""Test suite for PR-5 of the database review improvements.

Covers items 11 and 12 from the 2026-06-02 review:

  * Item 12 — full-DB backup via `sqlite3.Connection.backup()`.
    Exposed as `agent-mcp backup <project-dir> <output-path>` CLI
    subcommand. Online + WAL-safe — doesn't block writers.
  * Item 11 — `PRAGMA wal_checkpoint(PASSIVE)` + `PRAGMA optimize`
    helper, intended for periodic invocation. We test the helper
    directly; nightly scheduling is a one-line lifespan hookup.

Item 14 (tmux batching in view_status) is already implemented in
`agent_mcp/tools/admin_tools.py::view_status_tool_impl` — one
`list_tmux_sessions()` call, reused per agent. No code change
needed; the PR description notes the verification.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

# Synchronous tests (no harness): the CLI subcommand runs against a
# pre-built sqlite file; the helper runs against any connection.
# No pytest-asyncio markers needed.


# ---------------------------------------------------------------------------
# Item 12 — backup subcommand
# ---------------------------------------------------------------------------


def _seed_minimal_db(db_path: Path, n_rows: int = 5) -> None:
    """Populate `db_path` with a tiny `project_context` table.

    Just enough to verify a backup round-trips identical row count
    and the same `value` blobs; we don't need the full schema.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE project_context (
                context_key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT,
                created_at TEXT,
                created_by TEXT,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            );
            """
        )
        for i in range(n_rows):
            conn.execute(
                "INSERT INTO project_context "
                "(context_key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?)",
                (f"key-{i}", json.dumps({"i": i}), "t", "test"),
            )
        conn.commit()
    finally:
        conn.close()


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_mcp.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_backup_subcommand_round_trips_data(tmp_path: Path) -> None:
    """`agent-mcp backup <project> <out>` copies the DB byte-for-row.

    Online backup (`sqlite3.Connection.backup()`) is the canonical
    WAL-safe way; we don't compare byte-for-byte (WAL frames differ)
    but we compare row counts + every `(context_key, value)` pair.
    """
    project_dir = tmp_path / "proj"
    db_path = project_dir / ".agent" / "mcp_state.db"
    _seed_minimal_db(db_path, n_rows=7)

    out_path = tmp_path / "backup.db"
    result = _run_cli("backup", str(project_dir), str(out_path))
    assert result.returncode == 0, (
        f"backup subcommand exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert out_path.exists(), "backup file not created"

    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(out_path))
    try:
        src_rows = src.execute(
            "SELECT context_key, value FROM project_context "
            "ORDER BY context_key"
        ).fetchall()
        dst_rows = dst.execute(
            "SELECT context_key, value FROM project_context "
            "ORDER BY context_key"
        ).fetchall()
    finally:
        src.close()
        dst.close()
    assert dst_rows == src_rows, (
        f"backup row mismatch\n"
        f"  src: {src_rows}\n  dst: {dst_rows}"
    )


def test_backup_subcommand_refuses_to_overwrite(tmp_path: Path) -> None:
    """Don't silently overwrite an existing output file.

    Operators routinely re-run backups; if a fat-finger types the
    wrong path, we shouldn't clobber an existing backup. The
    subcommand exits non-zero and explains.
    """
    project_dir = tmp_path / "proj"
    db_path = project_dir / ".agent" / "mcp_state.db"
    _seed_minimal_db(db_path)

    out_path = tmp_path / "existing-backup.db"
    out_path.write_bytes(b"do not overwrite")

    result = _run_cli("backup", str(project_dir), str(out_path))
    assert result.returncode != 0, (
        "backup overwrote existing file; expected non-zero exit"
    )
    # File preserved.
    assert out_path.read_bytes() == b"do not overwrite"


def test_backup_subcommand_force_overwrites(tmp_path: Path) -> None:
    """`--force` opts in to overwriting."""
    project_dir = tmp_path / "proj"
    db_path = project_dir / ".agent" / "mcp_state.db"
    _seed_minimal_db(db_path, n_rows=3)

    out_path = tmp_path / "to-overwrite.db"
    out_path.write_bytes(b"old contents")

    result = _run_cli("backup", "--force", str(project_dir), str(out_path))
    assert result.returncode == 0, (
        f"--force backup failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # The new file is a valid sqlite DB, not the old bytes.
    conn = sqlite3.connect(str(out_path))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM project_context"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 3


def test_backup_subcommand_missing_db_errors(tmp_path: Path) -> None:
    """No db at the project path → non-zero with a clear message."""
    result = _run_cli(
        "backup", str(tmp_path / "no-such-project"), str(tmp_path / "out.db")
    )
    assert result.returncode != 0
    # Click's stock error text for a non-existent --type=Path arg is
    # "does not exist"; older click versions used "not found" or "no
    # such". Accept any of those phrasings so the test pins behaviour
    # without coupling to a specific click release.
    combined = (result.stderr + result.stdout).lower()
    assert (
        "does not exist" in combined
        or "not found" in combined
        or "no such" in combined
    ), f"unexpected error text: {combined!r}"


# ---------------------------------------------------------------------------
# Item 11 — wal_checkpoint + optimize helper
# ---------------------------------------------------------------------------


def test_wal_maintenance_helper_runs_clean(tmp_path: Path) -> None:
    """`run_wal_maintenance()` issues a PASSIVE checkpoint + optimize.

    We can't easily probe SQLite for "did a checkpoint actually
    happen?"; PRAGMA wal_checkpoint returns (busy, log, checkpointed)
    which we just confirm is a tuple of three ints. The helper must
    swallow OperationalError (no WAL file yet) gracefully.
    """
    from agent_mcp.db.maintenance import run_wal_maintenance

    db_path = tmp_path / "mcp_state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()

        # Helper should return a dict describing the run, not raise.
        result = run_wal_maintenance(conn)
        assert isinstance(result, dict)
        assert "checkpoint" in result
        # (busy, log, checkpointed) — three ints. busy==0 means the
        # checkpoint wasn't blocked by readers.
        cp = result["checkpoint"]
        assert isinstance(cp, tuple) and len(cp) == 3
        assert all(isinstance(x, int) for x in cp)
        # `optimize` returns no rows; helper should record success.
        assert result.get("optimize_ran") is True
    finally:
        conn.close()


def test_wal_maintenance_helper_swallows_on_closed_conn(tmp_path: Path) -> None:
    """Closed connection should produce a structured error, not crash."""
    from agent_mcp.db.maintenance import run_wal_maintenance

    db_path = tmp_path / "mcp_state.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()

    result = run_wal_maintenance(conn)
    assert isinstance(result, dict)
    assert result.get("error"), (
        f"expected error key in result for closed conn; got {result}"
    )
