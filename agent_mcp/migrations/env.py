# Alembic env.py for Agent-MCP.
#
# At runtime the agent-mcp lifespan calls
# `agent_mcp.db.migrations_runner.run_migrations_upgrade()`, which
# constructs a Config object pointed at the per-project sqlite file
# and invokes `command.upgrade(config, "head")`. That call ends up
# here.
#
# Manual invocations from the repo root (`alembic upgrade head`)
# also land here; they inherit MCP_PROJECT_DIR from the environment
# and we fall back to that when the runtime didn't set a URL.

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

# Make the agent_mcp package importable when alembic is invoked from
# the repo root. env.py lives at agent_mcp/migrations/env.py, so the
# repo root is three directories up.
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent_mcp.db.engine import Base  # noqa: E402
from agent_mcp.db import models  # noqa: E402,F401  — register tables on Base.metadata

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        # Logging config is optional; bail quietly if it's missing.
        pass

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Pick the sqlite URL Alembic should run against.

    Priority:
    1. URL already set on the Config (runtime path: agent-mcp's
       lifespan injects it via set_main_option).
    2. MCP_PROJECT_DIR environment variable (manual `alembic ...`).
    3. The placeholder from alembic.ini (last resort — usually a bug).
    """
    url = config.get_main_option("sqlalchemy.url")
    if url and not url.endswith("placeholder.db"):
        return url
    project_dir = os.environ.get("MCP_PROJECT_DIR")
    if project_dir:
        return f"sqlite:///{Path(project_dir).resolve() / '.agent' / 'mcp_state.db'}"
    return url or "sqlite:///./placeholder.db"


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

    # SQLite FK pragma policy for migrations (hotfix 2026-06-03):
    #
    # Earlier this file set `PRAGMA foreign_keys=ON` for the migration
    # connection on the theory that any FK-violating data the
    # migration introduced should fail loudly at migration time. That's
    # correct in the steady state — but it has a catastrophic
    # interaction with the batch_alter_table copy-and-rename dance
    # SQLite-Alembic uses for structural changes.
    #
    # When migration 0007 rebuilds `agents` to add `agents.current_task
    # -> tasks(task_id)`, the FK is in place. The subsequent rebuild
    # of `tasks` (to add `tasks.parent_task -> tasks(task_id)`) then
    # fails on `DROP TABLE tasks` because the `agents` FK references
    # it. Long-lived production DBs hit this on every deploy of
    # 0007/0008; CI's pristine schemas mask it.
    #
    # The fix is the SQLite-recommended pattern for any structural
    # migration: turn FKs OFF for the migration's duration, then
    # turn them back ON afterwards and run `foreign_key_check` as
    # the safety net. The migrations themselves (0007, 0008) do their
    # own orphan cleanup BEFORE the rebuild, so by the time FKs come
    # back on the data should already be FK-clean.
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
            # render_as_batch is required for sqlite ALTER operations
            # (column rename, drop, etc.) — set it here once so each
            # individual migration file doesn't have to.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()

        # Safety net: re-enable FKs and run `foreign_key_check` once
        # the migration transaction has committed. `PRAGMA foreign_keys`
        # can't be flipped inside an open transaction, so this runs
        # after `context.begin_transaction()` exits.
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        violations = connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if violations:
            raise RuntimeError(
                f"Foreign key violations after migrations: {violations}. "
                f"Migration left the DB in an inconsistent state — "
                f"investigate before retrying."
            )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
