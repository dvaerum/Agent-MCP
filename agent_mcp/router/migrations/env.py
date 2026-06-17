# Alembic env.py for the agent-mcp router-level identity DB.
#
# The router lifespan calls
# `agent_mcp.router.migrations_runner.run_router_migrations_upgrade()`,
# which constructs a Config pointed at the router DB and invokes
# `command.upgrade(config, "head")`. That call ends up here.
#
# The identity tables are simple enough that we keep them as raw-SQL
# `op.execute(...)` migrations rather than introducing a parallel
# ORM Base; the per-project migration tree already carries its own
# Base.metadata, and the router's three tables (users, sessions,
# project_membership) aren't shared with that schema.

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, event, pool

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        # Logging config is optional.
        pass

# No ORM metadata for the router schema today — autogenerate is not
# wired up, and migrations are hand-authored.
target_metadata = None


def _resolve_url() -> str:
    """Pick the sqlite URL Alembic should run against.

    Priority:
      1. URL already set on the Config (runtime path —
         `migrations_runner` injects it via set_main_option).
      2. AGENT_MCP_ROUTER_DB env var (manual `alembic` invocation).
      3. The placeholder from alembic.ini (last resort — usually a bug).
    """
    url = config.get_main_option("sqlalchemy.url")
    if url and not url.endswith("placeholder.db"):
        return url
    db_path = os.environ.get("AGENT_MCP_ROUTER_DB")
    if db_path:
        return f"sqlite:///{Path(db_path).resolve()}"
    return url or "sqlite:///./router-placeholder.db"


def run_migrations_offline() -> None:
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _resolve_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # Same FK-OFF-during-migration pattern as the per-project env.py
    # (agent_mcp/migrations/env.py). The router schema doesn't
    # currently use batch_alter_table rebuilds, but the policy is
    # cheap to keep consistent across both Alembic trees so a future
    # rebuild migration doesn't surprise us.
    @event.listens_for(connectable, "connect")
    def _set_pragmas(dbapi_connection, _record):  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=OFF")
        finally:
            cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()

        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        violations = connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if violations:
            raise RuntimeError(
                f"Foreign key violations after router migrations: {violations}. "
                f"Migration left router.db in an inconsistent state."
            )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
