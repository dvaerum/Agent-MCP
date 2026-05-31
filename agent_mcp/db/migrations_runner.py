# Agent-MCP/agent_mcp/db/migrations_runner.py
"""Programmatic Alembic invocation.

Application startup calls `run_migrations_upgrade()` after
`init_database()` so existing deployments pick up new migrations
without any user-visible step. The function is idempotent: running
it on an already-migrated DB is a no-op (Alembic compares
`alembic_version` against the available revisions).

We build an Alembic `Config` in memory instead of relying on cwd-
relative `alembic.ini` lookup so the path resolution works the same
whether agent-mcp is invoked from the repo root, the installed
console_script, or the Nix-wrapped binary.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from ..core.config import get_db_path, logger


def _find_migrations_dir() -> Path:
    """Locate the migrations/ directory shipped with the package.

    Lives alongside the package so it ships in both source-checkout
    and wheel-install layouts: `agent_mcp/migrations/`. The top-level
    `alembic.ini` points at this same directory for the manual CLI.
    """
    here = Path(__file__).resolve()
    # agent_mcp/db/migrations_runner.py → parent.parent == agent_mcp/.
    candidate = here.parent.parent / "migrations"
    if candidate.is_dir():
        return candidate

    raise FileNotFoundError(
        "Could not locate the agent-mcp migrations/ directory. "
        f"Looked at {candidate}."
    )


def _build_config() -> Config:
    """Construct an Alembic Config pointed at the current project DB."""
    migrations_dir = _find_migrations_dir()
    db_path = get_db_path()
    db_url = f"sqlite:///{db_path}"

    config = Config()
    config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def run_migrations_upgrade(revision: str = "head") -> None:
    """Run `alembic upgrade <revision>` against the current project DB.

    Safe to call repeatedly: Alembic does its own version-table
    comparison and skips already-applied revisions.
    """
    config = _build_config()
    db_path = get_db_path()
    logger.info(f"Applying Alembic migrations to {db_path} (target={revision})")
    command.upgrade(config, revision)
    logger.info("Alembic migrations applied.")
