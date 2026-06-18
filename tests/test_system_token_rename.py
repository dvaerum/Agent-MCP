"""RED tests for the admin_token → system_token rename (Phase 2 Wave 1b).

The rename is mechanical: the router-internal authority token, today
exposed as ``g.admin_token`` and accepted by ``verify_token(..., "admin")``,
becomes ``g.system_token`` and ``verify_token(..., "system")``.

Backwards-compatibility is required for one release:

* ``g.admin_token`` MUST keep returning the same value as
  ``g.system_token`` (the shim hides the underlying storage rename so
  every existing call site keeps working until they migrate).
* ``verify_token(token, "admin")`` MUST keep behaving identically to
  ``verify_token(token, "system")`` (deprecated alias).
* The legacy ``--admin-token-*`` CLI flags MUST keep working as aliases
  for the new ``--system-token-*`` flags. The deprecation warning
  surfaces on stderr (or Click's warning channel) so existing systemd
  units do not break instantly.
* The legacy DB key ``config_admin_token`` MUST be migrated to
  ``config_system_token`` on next boot (Alembic).

These tests pin all of the above. They are RED on origin/main (the
``system_token`` names do not exist yet) and turn GREEN once the rename
+ aliases land.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner


# ── globals / state shim ─────────────────────────────────────────────


def test_system_token_attribute_exists_on_globals() -> None:
    """``g.system_token`` is the new canonical attribute."""
    from agent_mcp.core import globals as g

    # Before init both are None; the attribute MUST exist.
    assert hasattr(g, "system_token")
    # And the legacy name MUST still resolve (alias).
    assert hasattr(g, "admin_token")


def test_setting_system_token_reads_back_on_admin_token_alias() -> None:
    """Writing to ``g.system_token`` must show up under ``g.admin_token``.

    The compatibility alias is the contract that keeps every existing
    caller (and external integrations) working until they migrate. The
    shim is one-storage-two-names; both reads MUST yield the same value.
    """
    from agent_mcp.core import globals as g

    prev_system = getattr(g, "system_token", None)
    prev_admin = g.admin_token
    try:
        g.system_token = "tok-from-system-side"
        assert g.system_token == "tok-from-system-side"
        assert g.admin_token == "tok-from-system-side"
    finally:
        g.system_token = prev_system
        g.admin_token = prev_admin


def test_setting_admin_token_reads_back_on_system_token_alias() -> None:
    """Writing to ``g.admin_token`` must show up under ``g.system_token``."""
    from agent_mcp.core import globals as g

    prev_system = getattr(g, "system_token", None)
    prev_admin = g.admin_token
    try:
        g.admin_token = "tok-from-admin-side"
        assert g.admin_token == "tok-from-admin-side"
        assert g.system_token == "tok-from-admin-side"
    finally:
        g.system_token = prev_system
        g.admin_token = prev_admin


# ── verify_token role alias ──────────────────────────────────────────


def test_verify_token_accepts_system_role() -> None:
    """``verify_token(token, "system")`` is the new canonical role check."""
    from agent_mcp.core import auth
    from agent_mcp.core import globals as g

    prev = g.admin_token
    try:
        g.admin_token = "sys-tok-xyz"
        assert auth.verify_token("sys-tok-xyz", "system") is True
        assert auth.verify_token("not-the-token", "system") is False
        assert auth.verify_token(None, "system") is False
    finally:
        g.admin_token = prev


def test_verify_token_admin_role_still_works_as_alias() -> None:
    """The legacy ``"admin"`` role MUST keep behaving as ``"system"``."""
    from agent_mcp.core import auth
    from agent_mcp.core import globals as g

    prev = g.admin_token
    try:
        g.admin_token = "sys-tok-legacy-alias"
        assert auth.verify_token("sys-tok-legacy-alias", "admin") is True
        assert auth.verify_token("not-the-token", "admin") is False
    finally:
        g.admin_token = prev


# ── application_startup: g.system_token gets populated ──────────────


def _run_startup(project_dir: Path, **kwargs) -> str:
    """Synchronously run application_startup against tmp project_dir
    and return the resolved system token.
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
    return g.system_token


def test_application_startup_sets_system_token(
    tmp_path: Path, reset_globals: None
) -> None:
    """After ``application_startup`` both names resolve to the same token."""
    from agent_mcp.core import globals as g

    project_dir = tmp_path / "p"
    project_dir.mkdir()

    token = _run_startup(project_dir)
    assert token, "expected a non-empty system token after startup"
    assert g.system_token == token
    assert g.admin_token == token


# ── DB key migration: config_admin_token → config_system_token ──────


def test_db_key_migrated_to_config_system_token(
    tmp_path: Path, reset_globals: None
) -> None:
    """Fresh-boot persists the token under ``config_system_token``."""
    project_dir = tmp_path / "p"
    project_dir.mkdir()

    token = _run_startup(project_dir)

    db_path = project_dir / ".agent" / "mcp_state.db"
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT value FROM project_context WHERE context_key = ?",
            ("config_system_token",),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, (
        "expected config_system_token to be persisted; got no row"
    )
    assert json.loads(row[0]) == token


def test_existing_config_admin_token_is_migrated(
    tmp_path: Path, reset_globals: None
) -> None:
    """A pre-existing ``config_admin_token`` row is renamed in place.

    Simulates an upgrade from an older agent-mcp install: the project DB
    has the token persisted under the legacy key. After boot, the row
    MUST exist under ``config_system_token`` and the in-memory token
    MUST match.
    """
    from agent_mcp.db.schema import initialize_database_schema
    from agent_mcp.db import engine as _engine
    import os

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    # Seed the DB with the legacy key BEFORE lifespan runs.
    os.environ["MCP_PROJECT_DIR"] = str(project_dir)
    (project_dir / ".agent").mkdir(parents=True, exist_ok=True)
    initialize_database_schema()
    db_path = project_dir / ".agent" / "mcp_state.db"

    legacy_token = "legacy-system-token-abc123"
    conn = sqlite3.connect(db_path)
    try:
        import datetime as _dt
        now = _dt.datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO project_context
              (context_key, value, description, created_at, created_by,
               updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "config_admin_token",
                json.dumps(legacy_token),
                "Persistent MCP Admin Token (legacy)",
                now,
                "test-seed",
                now,
                "test-seed",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Drop the cached engine so the next get_db_path is re-resolved
    # under the test's project_dir.
    _engine.reset_engine_cache()

    token = _run_startup(project_dir)
    assert token == legacy_token, (
        "expected the lifecycle to pick up the legacy config_admin_token "
        f"value, got {token!r}"
    )

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT context_key FROM project_context "
            "WHERE context_key IN ('config_admin_token', 'config_system_token')"
        )
        keys = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    assert "config_system_token" in keys, (
        "expected migration to introduce config_system_token row"
    )


# ── CLI flag rename + deprecation aliases ───────────────────────────


def test_server_help_lists_system_token_flags() -> None:
    """The new ``--system-token-*`` flags MUST surface in --help."""
    from agent_mcp.cli import server_cmd

    runner = CliRunner()
    result = runner.invoke(server_cmd, ["--help"])
    assert result.exit_code == 0, result.output
    for flag in (
        "--system-token",
        "--system-token-out",
        "--system-token-in",
        "--system-token-log",
        "--system-token-format",
    ):
        assert flag in result.output, (
            f"expected {flag} in --help; got:\n{result.output}"
        )


def test_system_token_out_writes_token_in_raw_format(
    tmp_path: Path, reset_globals: None
) -> None:
    """``--system-token-out FILE`` writes the token in the same shape
    ``--admin-token-out FILE`` did (just the token, newline-terminated,
    mode 0600)."""
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    out_path = tmp_path / "tok.out"

    token = _run_startup(
        project_dir,
        system_token_out_path=str(out_path),
        system_token_out_format="raw",
    )
    assert out_path.exists()
    content = out_path.read_text()
    assert content.strip() == token
    assert "MCP_SYSTEM_TOKEN=" not in content
    assert "MCP_ADMIN_TOKEN=" not in content
    mode = out_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected mode 0600, got {oct(mode)}"


def test_system_token_out_writes_env_assignment(
    tmp_path: Path, reset_globals: None
) -> None:
    """``--system-token-out FILE --system-token-format env`` writes
    ``MCP_SYSTEM_TOKEN=<token>``."""
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    out_path = tmp_path / "tok.env"

    token = _run_startup(
        project_dir,
        system_token_out_path=str(out_path),
        system_token_out_format="env",
    )
    content = out_path.read_text()
    assert content == f"MCP_SYSTEM_TOKEN={token}\n"


def test_legacy_admin_token_out_still_works_as_alias(
    tmp_path: Path, reset_globals: None
) -> None:
    """The deprecated ``--admin-token-out`` flag MUST keep functioning
    so existing systemd units do not break instantly."""
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    out_path = tmp_path / "tok.out.legacy"

    token = _run_startup(
        project_dir,
        admin_token_out_path=str(out_path),
        admin_token_out_format="raw",
    )
    assert out_path.exists()
    assert out_path.read_text().strip() == token
