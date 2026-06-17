"""CLI surface + lifecycle behavior for the admin-token I/O flags.

Replaces the always-on `logger.info("MCP Admin Token: <token>")` line
with four explicit opt-in flags:

* `--admin-token-out PATH`  — write the token to PATH after resolution
* `--admin-token-format {raw,env}` — formatting for --admin-token-out
* `--admin-token-in PATH`   — read the token from PATH at startup
* `--admin-token-log`       — log the token to stdout/log on startup

At most one of `-out` / `-in` / `-log` may be set.
`--admin-token-format` is only meaningful with `-out`.

The CLI-surface tests use CliRunner (no real server boot).
The behavior tests drive `application_startup` directly with a tmp DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Tuple

import pytest
from click.testing import CliRunner


# ── CLI surface (validation only — no server boot) ────────────────────


def test_server_help_lists_new_flags() -> None:
    from agent_mcp.cli import server_cmd

    runner = CliRunner()
    result = runner.invoke(server_cmd, ["--help"])
    assert result.exit_code == 0, result.output
    for flag in (
        "--admin-token-out",
        "--admin-token-in",
        "--admin-token-log",
        "--admin-token-format",
    ):
        assert flag in result.output, f"expected {flag} in --help; got:\n{result.output}"


def test_out_and_in_are_mutually_exclusive(tmp_path: Path) -> None:
    from agent_mcp.cli import server_cmd

    runner = CliRunner()
    out = tmp_path / "out.txt"
    in_ = tmp_path / "in.txt"
    in_.write_text("tok-x\n")
    result = runner.invoke(
        server_cmd,
        [
            "--admin-token-out",
            str(out),
            "--admin-token-in",
            str(in_),
            "--project-dir",
            str(tmp_path),
            "--no-tui",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower() or "only one" in result.output.lower()


def test_out_and_log_are_mutually_exclusive(tmp_path: Path) -> None:
    from agent_mcp.cli import server_cmd

    runner = CliRunner()
    out = tmp_path / "out.txt"
    result = runner.invoke(
        server_cmd,
        [
            "--admin-token-out",
            str(out),
            "--admin-token-log",
            "--project-dir",
            str(tmp_path),
            "--no-tui",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower() or "only one" in result.output.lower()


def test_format_requires_out(tmp_path: Path) -> None:
    from agent_mcp.cli import server_cmd

    runner = CliRunner()
    result = runner.invoke(
        server_cmd,
        [
            "--admin-token-format",
            "env",
            "--project-dir",
            str(tmp_path),
            "--no-tui",
        ],
    )
    assert result.exit_code != 0
    assert (
        "--admin-token-out" in result.output
        or "requires" in result.output.lower()
    )


# ── application_startup behavior (real DB, no Starlette) ──────────────


def _run_startup(
    project_dir: Path,
    **kwargs,
) -> str:
    """Synchronously run application_startup against tmp project_dir
    and return the resolved admin token (g.admin_token).

    Resets globals so this can be called multiple times in one test session.
    """
    from agent_mcp.app.server_lifecycle import application_startup
    from agent_mcp.core import globals as g
    from agent_mcp.db import write_queue as _wq
    from agent_mcp.db import engine as _engine

    _wq._global_write_queue = None
    _engine.reset_engine_cache()
    g.reset_startup_complete_event()
    g.admin_token = None

    asyncio.run(
        application_startup(
            project_dir_path_str=str(project_dir),
            **kwargs,
        )
    )
    return g.admin_token


def test_admin_token_in_reads_file_and_overrides_db(
    tmp_path: Path, reset_globals: None
) -> None:
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    token_in = tmp_path / "tok.in"
    token_in.write_text("supplied-token-abc\n")

    token = _run_startup(project_dir, admin_token_in_path=str(token_in))
    assert token == "supplied-token-abc"

    # And it must be persisted to the DB.
    db_path = project_dir / ".agent" / "mcp_state.db"
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT value FROM project_context WHERE context_key = ?",
            ("config_admin_token",),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert json.loads(row[0]) == "supplied-token-abc"


def test_admin_token_out_raw_writes_token_only(
    tmp_path: Path, reset_globals: None
) -> None:
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    out_path = tmp_path / "tok.out"

    token = _run_startup(
        project_dir,
        admin_token_out_path=str(out_path),
        admin_token_out_format="raw",
    )
    assert out_path.exists()
    content = out_path.read_text()
    assert content.strip() == token
    assert "MCP_ADMIN_TOKEN=" not in content
    # File mode should be 0600.
    mode = out_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected mode 0600, got {oct(mode)}"


def test_admin_token_out_env_writes_env_assignment(
    tmp_path: Path, reset_globals: None
) -> None:
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    out_path = tmp_path / "tok.env"

    token = _run_startup(
        project_dir,
        admin_token_out_path=str(out_path),
        admin_token_out_format="env",
    )
    content = out_path.read_text()
    assert content == f"MCP_ADMIN_TOKEN={token}\n"


def test_admin_token_log_logs_token(
    tmp_path: Path, reset_globals: None, caplog: pytest.LogCaptureFixture
) -> None:
    project_dir = tmp_path / "p"
    project_dir.mkdir()

    with caplog.at_level(logging.INFO, logger="agent_mcp.app.server_lifecycle"):
        token = _run_startup(project_dir, admin_token_log=True)

    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert token in joined, "expected token in log output when --admin-token-log set"


def test_default_no_token_in_logs_or_files(
    tmp_path: Path, reset_globals: None, caplog: pytest.LogCaptureFixture
) -> None:
    project_dir = tmp_path / "p"
    project_dir.mkdir()

    with caplog.at_level(logging.INFO, logger="agent_mcp.app.server_lifecycle"):
        token = _run_startup(project_dir)

    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert token not in joined, (
        "default startup must not leak the admin token into logs; "
        f"found token {token!r} in log records"
    )
    # And no stray token file should have been written under tmp_path.
    for entry in tmp_path.rglob("*"):
        if entry.is_file() and entry.suffix not in {".db", ".log", ".json", ".jsonl"}:
            assert token not in entry.read_text(errors="ignore"), (
                f"found token leaked into {entry}"
            )
