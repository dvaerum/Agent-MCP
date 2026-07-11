# Agent-MCP/mcp_template/mcp_server_src/app/server_lifecycle.py
import os
import sys
import json
import datetime
import sqlite3
import anyio  # For managing background tasks
from pathlib import Path
from dotenv import load_dotenv

# Project-specific imports
from ..core.config import logger, get_project_dir
from ..core import globals as g
from ..utils.project_utils import init_agent_directory
from ..db.schema import init_database as initialize_database_schema
from ..db.connection import get_db_connection, check_vss_loadability
from ..db.migrations_runner import run_migrations_upgrade
from ..external.openai_service import initialize_openai_client
from ..features.rag.indexing import run_rag_indexing_periodically

from ..features.claude_session_monitor import run_claude_session_monitoring
from ..features.message_retention import (
    DEFAULT_INTERVAL_SECONDS as MESSAGE_RETENTION_INTERVAL_DEFAULT,
    run_message_retention_periodically,
)
from ..features.session_registry_pruner import (
    DEFAULT_INTERVAL_SECONDS as SESSION_REGISTRY_INTERVAL_DEFAULT,
    DEFAULT_THRESHOLD_SECONDS as SESSION_REGISTRY_THRESHOLD_DEFAULT,
    run_session_registry_pruner,
)
from ..utils.signal_utils import register_signal_handlers  # For graceful shutdown


# Wave 4 (cleanup/wave-4-delete-admin-pseudo-agent): the synthetic
# admin pseudo-agent row (agent_id='admin') and its lifespan-startup
# backstop helper (`_ensure_admin_pseudo_agent_row`) were deleted
# here as part of admin_token retirement. Migration 0014
# (`0014_drop_admin_pseudo_agent`) drops the row and the FKs that
# pinned it in place. retire-system-token Wave 3 deleted the
# system-token plumbing entirely (helpers, CLI flags, persistence
# block, and the `g.system_token` global it populated).


# This function encapsulates the logic originally in main() before server run.
async def application_startup(
    project_dir_path_str: str,
):
    """
    Handles all application startup procedures:
    - Sets project directory environment variable.
    - Initializes .agent directory.
    - Initializes database schema.
    - Loads existing state (agents, tasks) from DB.
    - Initializes external services (OpenAI client).
    - Performs VSS loadability check.
    - Registers signal handlers.
    """
    # Load environment variables from .env file
    load_dotenv()

    logger.info("MCP Server application starting up...")
    g.server_start_time = datetime.datetime.now().isoformat()  # For uptime calculation

    # 1. Handle Project Directory (Original main.py:1950-1959)
    project_path = Path(project_dir_path_str).resolve()
    if not project_path.exists():
        logger.info(f"Project directory '{project_path}' does not exist. Creating it.")
        try:
            project_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(
                f"CRITICAL: Failed to create project directory '{project_path}': {e}. Exiting."
            )
            raise SystemExit(f"Failed to create project directory: {e}") from e
    elif not project_path.is_dir():
        logger.error(
            f"CRITICAL: Project path '{project_path}' is not a directory. Exiting."
        )
        raise SystemExit(f"Project path '{project_path}' is not a directory.")

    os.environ["MCP_PROJECT_DIR"] = str(
        project_path
    )  # Critical for other modules using get_project_dir()
    logger.info(f"Using project directory: {project_path}")

    # 2. Initialize .agent directory (Original main.py:1962-1966)
    agent_dir = init_agent_directory(
        str(project_path)
    )  # project_utils.init_agent_directory
    if agent_dir is None:  # init_agent_directory returns None on critical failure
        logger.error(
            "CRITICAL: Failed to initialize .agent directory structure. Exiting."
        )
        raise SystemExit("Failed to initialize .agent directory.")
    logger.info(f".agent directory initialized at {agent_dir}")

    # 3. Initialize Database Schema (Original main.py:1969-1974)
    try:
        initialize_database_schema()  # db.schema.init_database
    except Exception as e:
        logger.error(
            f"CRITICAL: Failed to initialize database: {e}. Exiting.", exc_info=True
        )
        # DB_FILE_NAME is in core.config, get_db_path uses it.
        from ..core.config import get_db_path as get_db_path_for_error

        db_path_err = get_db_path_for_error()  # Get path for error message
        raise SystemExit(
            f"Error: Failed to initialize database at {db_path_err}. Check logs and permissions."
        ) from e

    # 3a. Apply Alembic migrations (Phase 7a). init_database() above
    # still owns CREATE TABLE for fresh DBs; this picks up any model-
    # level schema changes (column adds/renames, new tables created
    # via ORM in later PRs). Idempotent — a re-run on an already-
    # migrated DB is a no-op.
    try:
        run_migrations_upgrade()
    except Exception as e:
        logger.error(
            f"CRITICAL: Failed to apply Alembic migrations: {e}. Exiting.",
            exc_info=True,
        )
        raise SystemExit(
            f"Error: Failed to apply Alembic migrations. Check logs."
        ) from e

    # 3b. (Wave 4) The synthetic 'admin' pseudo-agent row that lived
    # here from migrations 0008 → 0013 has been retired. Migration
    # 0014 deletes it and the FK constraints that previously required
    # it; the system bearer no longer needs an agents-table parent.
    # See migration 0014_drop_admin_pseudo_agent for the full rationale.

    # 5. Load existing state from Database (Original main.py:2015-2045)
    logger.info("Loading existing state from database...")
    conn_load_state = None
    try:
        conn_load_state = get_db_connection()
        cursor = conn_load_state.cursor()

        # Load Active Agents into the g.active_agents auth cache.
        # "Active" excludes BOTH 'terminated' (soft-deleted) AND
        # 'tombstone' (BL-R31-3b): a purge inserts a durable
        # status='tombstone' agents row (`[deleted-<id>]`, reserved
        # `__tombstone_<id>` token) as an FK target, and that row
        # SURVIVES restarts — so a plain `status != 'terminated'` load
        # would pull it into the auth allow-list on the next boot after
        # a purge, from where the operator token listing (which filters
        # only `!= 'terminated'`) would leak it. Wave 4: the synthetic
        # 'admin' row is gone (migration 0014), so the earlier
        # `agent_id != 'admin'` filter is redundant.
        active_agents_count = 0
        from ..repositories.agent_repository import LIVE_AGENT_SQL

        cursor.execute(
            f"""
            SELECT token, agent_id, capabilities, created_at, status, current_task, working_directory, color
            FROM agents WHERE {LIVE_AGENT_SQL}
        """,
        )
        for row in cursor.fetchall():
            agent_token_val = row["token"]
            agent_id_val = row["agent_id"]
            g.active_agents[agent_token_val] = {
                "agent_id": agent_id_val,
                "capabilities": json.loads(row["capabilities"] or "[]"),
                "created_at": row["created_at"],
                "status": row["status"],
                "current_task": row["current_task"],
                "color": row["color"],  # Added color loading
            }
            g.agent_working_dirs[agent_id_val] = row["working_directory"]
            active_agents_count += 1
        logger.info(f"Loaded {active_agents_count} active agents from database.")
        # Echo to stderr so operators see this in `journalctl` without
        # needing MCP_DEBUG=true. This is the single most important
        # operational signal for the worker-auth-401 class of bugs: if
        # this number is 0 (or missing entirely from the journal), no
        # worker bearer will authenticate post-restart. Mirrors the
        # banner prints above which already go to stdout unconditionally.
        print(
            f"📡 Loaded {active_agents_count} active agent(s) into auth allow-list.",
            file=sys.stderr,
            flush=True,
        )

        # Load All Tasks into g.tasks
        task_count = 0
        cursor.execute("SELECT * FROM tasks")  # Load all tasks
        for row_dict in (dict(row) for row in cursor.fetchall()):
            task_id_val = row_dict["task_id"]
            # Ensure complex fields are Python lists/dicts in memory
            for field_key in ["child_tasks", "depends_on_tasks", "notes"]:
                if isinstance(row_dict.get(field_key), str):
                    try:
                        row_dict[field_key] = json.loads(row_dict[field_key] or "[]")
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Failed to parse JSON for field '{field_key}' in task '{task_id_val}'. Defaulting to empty list."
                        )
                        row_dict[field_key] = []
            g.tasks[task_id_val] = row_dict
            task_count += 1
        logger.info(f"Loaded {task_count} tasks into memory cache.")

        # File map (g.file_map) and audit log (g.audit_log) are transient and start empty.
        g.file_map.clear()
        g.audit_log.clear()
        logger.info(
            "In-memory file map and audit log initialized as empty for this session."
        )

    except sqlite3.Error as e_sql_load:
        logger.error(
            f"Database error during state loading: {e_sql_load}. Server might operate with incomplete state.",
            exc_info=True,
        )
        # Decide if this is critical. Original proceeded with empty state.
        g.active_agents.clear()
        g.tasks.clear()
        g.agent_working_dirs.clear()
    except Exception as e_load:
        logger.error(
            f"Unexpected error during state loading: {e_load}. Exiting.", exc_info=True
        )
        raise SystemExit(f"Unexpected error loading state: {e_load}") from e_load
    finally:
        if conn_load_state:
            conn_load_state.close()
    logger.info("State loading from database complete.")

    # 6. Initialize OpenAI Client (Original main.py: part of get_openai_client, called by RAG indexer)
    # We explicitly initialize it at startup now.
    if (
        not initialize_openai_client()
    ):  # external.openai_service.initialize_openai_client
        logger.warning(
            "OpenAI client failed to initialize. OpenAI-dependent features (like RAG) will be unavailable."
        )
        # Server can continue, but RAG won't work.

    # 6.6. Install Repository singletons (PR #146).
    # The class-based ``TaskRepository`` is the single owner of the
    # ``state.tasks`` cache + DB invariant; installing here (after the
    # DB schema is applied and the write queue is running) means every
    # request handler can ``from agent_mcp.repositories import task_repo``
    # without worrying about cold-start races. The teardown counterpart
    # lives in ``application_shutdown`` so a stale instance bound to a
    # closed engine doesn't leak across the lifespan boundary.
    from ..repositories import (
        set_agent_repo,
        set_message_repo,
        set_rag_repo,
        set_task_repo,
    )
    from ..repositories.agent_repository import AgentRepository
    from ..repositories.message_repository import MessageRepository
    from ..repositories.rag_repository import RagRepository
    from ..repositories.task_repository import TaskRepository

    set_task_repo(TaskRepository())
    logger.info("TaskRepository singleton installed.")
    set_agent_repo(AgentRepository())
    logger.info("AgentRepository singleton installed.")
    set_message_repo(MessageRepository())
    logger.info("MessageRepository singleton installed.")
    # PR F of round 2 — RagRepository is the single owner of
    # rag_chunks / rag_embeddings / rag_meta. Installed alongside the
    # other three concept repos so call sites (features/rag/indexing
    # and features/rag/query post-migration) can resolve it via
    # ``from agent_mcp.repositories import rag_repo`` with no startup-
    # order constraint.
    set_rag_repo(RagRepository())
    logger.info("RagRepository singleton installed.")

    # 7. Perform VSS Loadability Check (Original main.py: called by init_database)
    # This ensures g.global_vss_load_successful is set.
    check_vss_loadability()  # db.connection.check_vss_loadability
    if g.global_vss_load_successful:
        logger.info("sqlite-vec (VSS) extension confirmed loadable.")
    else:
        logger.warning(
            "sqlite-vec (VSS) extension is NOT loadable. RAG search functionality will be impaired."
        )

    # 8. Register Signal Handlers (Original main.py: 839-840, called before server run)
    register_signal_handlers()  # utils.signal_utils.register_signal_handlers

    # 9. Signal startup-complete to background tasks.
    # The CLI's SSE-mode runner (cli.py::run_sse_server_with_bg_tasks)
    # awaits `start_background_tasks(tg)` BEFORE `server.serve()`, which
    # is the call that triggers Starlette's lifespan → this function.
    # So bg tasks (session-registry pruner, message-retention pruner,
    # RAG indexer, claude-session monitor) are already scheduled by the
    # time we get here — but they MUST NOT fire their first cycle until
    # `MCP_PROJECT_DIR` is set (step 1 above) and the DB schema + Alembic
    # migrations are applied (steps 3 + 3a). Without this gate, the
    # pruner's SQLAlchemy
    # `get_engine()` call resolves the DB path via the fallback
    # `Path(".")` and the engine cache binds to a wrong / empty
    # bystander file — every subsequent ORM query then sees "no such
    # table: …" against that file.
    g.startup_complete_event.set()
    logger.info("MCP Server application startup sequence finished.")


async def start_background_tasks(task_group: anyio.abc.TaskGroup):
    """Starts long-running background tasks like the RAG indexer."""
    logger.info("Starting background tasks...")
    # Start RAG Indexer (Original main.py: 2625-2627)
    # The interval can be made configurable if needed.
    rag_interval = int(os.environ.get("MCP_RAG_INDEX_INTERVAL_SECONDS", "300"))
    g.rag_index_task_scope = await task_group.start(
        run_rag_indexing_periodically, rag_interval
    )
    logger.info(f"RAG indexing task started with interval {rag_interval}s.")

    # Start Claude Code Session Monitor
    claude_session_interval = int(
        os.environ.get("MCP_CLAUDE_SESSION_MONITOR_INTERVAL", "5")
    )
    g.claude_session_task_scope = await task_group.start(
        run_claude_session_monitoring, claude_session_interval
    )
    logger.info(
        f"Claude Code session monitor started with interval {claude_session_interval}s."
    )

    # Start agent_messages retention pruner (Phase 6 follow-up, issue Q).
    # The interval is long (24h default) — pruning is bookkeeping, not
    # latency-sensitive. Override via MCP_MESSAGE_RETENTION_INTERVAL_SECONDS
    # for tests / ops.
    retention_interval = int(
        os.environ.get(
            "MCP_MESSAGE_RETENTION_INTERVAL_SECONDS",
            str(MESSAGE_RETENTION_INTERVAL_DEFAULT),
        )
    )
    g.message_retention_task_scope = await task_group.start(
        run_message_retention_periodically, retention_interval
    )
    logger.info(
        f"Message retention pruner started with interval {retention_interval}s."
    )

    # Start session registry pruner (cross-request notification fan-out
    # plumbing — Phase: session-registry). Sweeps mcp_sessions for rows
    # whose `last_seen_at` is older than threshold so a crashed /
    # disconnected GET /mcp stream doesn't keep getting fanned out to.
    session_registry_interval = int(
        os.environ.get(
            "MCP_SESSION_REGISTRY_INTERVAL_SECONDS",
            str(SESSION_REGISTRY_INTERVAL_DEFAULT),
        )
    )
    session_registry_threshold = int(
        os.environ.get(
            "MCP_SESSION_REGISTRY_THRESHOLD_SECONDS",
            str(SESSION_REGISTRY_THRESHOLD_DEFAULT),
        )
    )
    g.session_registry_pruner_task_scope = await task_group.start(
        run_session_registry_pruner,
        session_registry_interval,
        session_registry_threshold,
    )
    logger.info(
        "Session registry pruner started "
        f"(interval={session_registry_interval}s, "
        f"threshold={session_registry_threshold}s)."
    )


async def application_shutdown():
    """Handles graceful shutdown of application resources and tasks."""
    logger.info("MCP Server application shutting down...")
    g.server_running = False  # Ensure flag is set for all components

    # Cancel background tasks
    if g.rag_index_task_scope and not g.rag_index_task_scope.cancel_called:
        logger.info("Attempting to cancel RAG indexing task...")
        g.rag_index_task_scope.cancel()

    if g.claude_session_task_scope and not g.claude_session_task_scope.cancel_called:
        logger.info("Attempting to cancel Claude session monitoring task...")
        g.claude_session_task_scope.cancel()
        # Note: Actual waiting for task completion is usually handled by the AnyIO TaskGroup context manager.

    if (
        g.message_retention_task_scope
        and not g.message_retention_task_scope.cancel_called
    ):
        logger.info("Attempting to cancel message retention pruner task...")
        g.message_retention_task_scope.cancel()

    if (
        getattr(g, "session_registry_pruner_task_scope", None)
        and not g.session_registry_pruner_task_scope.cancel_called
    ):
        logger.info("Attempting to cancel session registry pruner task...")
        g.session_registry_pruner_task_scope.cancel()

    # Clear Repository singletons (PR #146) so a subsequent app build
    # in the same process (test harness, hot reload) gets a fresh
    # instance bound to the new engine cache rather than the stale one
    # this lifespan just tore down.
    try:
        from ..repositories import (
            clear_agent_repo,
            clear_message_repo,
            clear_rag_repo,
            clear_task_repo,
        )

        clear_task_repo()
        logger.info("TaskRepository singleton cleared.")
        clear_agent_repo()
        logger.info("AgentRepository singleton cleared.")
        clear_message_repo()
        logger.info("MessageRepository singleton cleared.")
        clear_rag_repo()
        logger.info("RagRepository singleton cleared.")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Failed to clear Repository singletons: {e}")

    # Add any other cleanup (e.g., closing persistent connections if not managed by context)
    # For SQLite, connections are typically short-lived per request/operation.

    logger.info("MCP Server application shutdown sequence complete.")


# These functions will be used by the Starlette app's `on_startup` and `on_shutdown` events,
# or by the CLI runner.
