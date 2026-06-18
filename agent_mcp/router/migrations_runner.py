"""Programmatic Alembic invocation for the router-level identity DB.

The router lifespan (in `agent_mcp.router.identity.init_router_db`)
calls `run_router_migrations_upgrade()` before bootstrap; that
function builds an Alembic `Config` pointed at the router DB and
runs `command.upgrade(config, "head")`. Idempotent.

Mirrors the pattern in `agent_mcp.db.migrations_runner`, but targets
a different DB path (router.db) and a different migrations tree
(agent_mcp/router/migrations/).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config


logger = logging.getLogger(__name__)


# Default DB path for production deploys. Overridable via the
# AGENT_MCP_ROUTER_DB env var (used by tests to point at tmp paths
# and by the home-manager module to point at /var/lib/agent-mcp/).
_DEFAULT_ROUTER_DB = Path("/var/lib/agent-mcp/router.db")


def get_router_db_path() -> Path:
    """Return the router DB path.

    Resolution order:
      1. AGENT_MCP_ROUTER_DB env var (test isolation; ops override).
      2. /var/lib/agent-mcp/router.db (production default).
    """
    override = os.environ.get("AGENT_MCP_ROUTER_DB")
    if override:
        return Path(override).resolve()
    return _DEFAULT_ROUTER_DB


def _find_migrations_dir() -> Path:
    """Locate the router migrations/ directory shipped with the package."""
    here = Path(__file__).resolve()
    # agent_mcp/router/migrations_runner.py → parent == agent_mcp/router/
    candidate = here.parent / "migrations"
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(
        f"Could not locate router migrations directory at {candidate}."
    )


def _build_config(db_path: Path) -> Config:
    """Construct an Alembic Config pointed at the router DB."""
    migrations_dir = _find_migrations_dir()
    db_url = f"sqlite:///{db_path}"
    config = Config()
    config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def run_router_migrations_upgrade(revision: str = "head") -> None:
    """Apply Alembic migrations to the router DB.

    Safe to call repeatedly. Creates the parent directory if it
    doesn't exist — production deploys typically pre-create
    /var/lib/agent-mcp via systemd's StateDirectory, but tests and
    one-off invocations benefit from the autocreate.
    """
    db_path = get_router_db_path()
    # Production deploys provision /var/lib/agent-mcp via systemd
    # tmpfiles/StateDirectory, owned by the service user; the
    # parent /var/lib is root-only-writable, so a fallback mkdir
    # from inside the service process can't create siblings of
    # already-existing dirs. If the leaf already exists with the
    # right owner (the production case), this swallowed
    # PermissionError is the no-op we want; if it doesn't exist
    # and we can't create it, re-raise so the operator sees the
    # missing-path problem instead of a confusing later sqlite
    # error.
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        if not db_path.parent.exists():
            raise
    config = _build_config(db_path)
    logger.info(
        "Applying Alembic migrations to router DB %s (target=%s)",
        db_path,
        revision,
    )
    command.upgrade(config, revision)
    logger.info("Router migrations applied.")
