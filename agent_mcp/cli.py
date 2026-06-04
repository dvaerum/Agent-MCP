#!/usr/bin/env python3
"""
Agent-MCP CLI: Command-line interface for multi-agent collaboration.

Copyright (C) 2025 Luis Alejandro Rincon (rinadelph)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import click
import uvicorn  # For running the Starlette app in SSE mode
import anyio  # For running async functions and task groups
import os
import sys
import json
import sqlite3
import warnings
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

# Load environment variables before importing other modules
# Try explicit paths

# Get the directory of the current script
script_dir = Path(__file__).resolve().parent

# Try parent directories
for parent_level in range(3):  # Go up to 3 levels
    env_path = script_dir / (".." * parent_level) / ".env"
    env_path = env_path.resolve()
    print(f"Trying to load .env from: {env_path}")
    if env_path.exists():
        print(f"Found .env at: {env_path}")
        env_vars = dotenv_values(str(env_path))
        print(f"Loaded variables: {list(env_vars.keys())}")
        print(
            f"OPENAI_API_KEY from file: {env_vars.get('OPENAI_API_KEY', 'NOT FOUND')[:10]}..."
        )
        # Manually set the environment variables
        for key, value in env_vars.items():
            os.environ[key] = value
        # Check if API key was set (without logging the actual key)
        api_key = os.environ.get('OPENAI_API_KEY')
        if api_key:
            print("OPENAI_API_KEY successfully loaded from environment")
        else:
            print("OPENAI_API_KEY not found in environment")
        break

# Also try normal load_dotenv in case
load_dotenv()

# Project-specific imports
# Ensure core.config (and thus logging) is initialized early.
from .core.config import (
    logger,
    CONSOLE_LOGGING_ENABLED,
    enable_console_logging,
)  # Logger is initialized in config.py
from .core import globals as g  # For g.server_running and other globals

# Import app creation and lifecycle functions
from .app.main_app import create_app, mcp_app_instance  # mcp_app_instance for stdio
from .app.server_lifecycle import (
    start_background_tasks,
    application_startup,
    application_shutdown,
)  # application_startup is called by create_app's on_startup
from .tui.display import TUIDisplay  # Import TUI display


def get_admin_token_from_db(project_dir: str) -> Optional[str]:
    """Get the admin token from the SQLite database."""
    try:
        # Construct the path to the database
        db_path = Path(project_dir).resolve() / ".agent" / "mcp_state.db"

        if not db_path.exists():
            return None

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get the admin token from project_context table
        cursor.execute(
            "SELECT value FROM project_context WHERE context_key = ?",
            ("config_admin_token",),
        )
        row = cursor.fetchone()

        if row and row["value"]:
            try:
                admin_token = json.loads(row["value"])
                if isinstance(admin_token, str) and admin_token:
                    return admin_token
            except json.JSONDecodeError:
                pass

        conn.close()
        return None
    except Exception as e:
        logger.error(f"Error reading admin token from database: {e}")
        return None


# --- Click Command Group ---
# Top-level dispatcher. Two subcommands today:
#   * `agent-mcp server …`  — the MCP backend (Starlette/uvicorn or stdio).
#   * `agent-mcp router …`  — the always-on URL-keyed HTTP router that
#                              proxies per-project backends.
# Phase 1a of the router-upstream plan (prancy-napping-pie). Before
# this the CLI had a single `@click.command` whose options matched
# today's `server` subcommand exactly; we keep a backward-compat shim
# below so existing `python -m agent_mcp.cli --transport sse …`
# invocations route to `server` and warn loudly.
@click.group(
    context_settings=dict(help_option_names=["-h", "--help"]),
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Agent-MCP command-line interface.

    Run `agent-mcp server --help` for the MCP backend, or
    `agent-mcp router --help` for the always-on HTTP router.
    """
    # Subcommand will run on its own. When invoked with no
    # subcommand and no args at all, default to `server` for
    # backward compat — same as pre-Phase-1a behaviour. Tests rely
    # on `--help` printing the group help, which click handles
    # before we get here.
    if ctx.invoked_subcommand is None:
        # Empty invocation: keep the historic behaviour of starting
        # the server with defaults. Emit a deprecation note so we
        # can remove this in a future release.
        warnings.warn(
            "Invoking agent-mcp with no subcommand is deprecated; "
            "use 'agent-mcp server' (or 'agent-mcp router') instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        ctx.invoke(server_cmd)


# --- `server` subcommand ---
# This replicates the original @click.command's options exactly so
# pre-Phase-1a invocations keep working once routed through the
# backward-compat shim below.
@cli.command("server", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option(
    "--port",
    type=int,
    default=os.environ.get("PORT", 8080),  # Read from env var PORT if set, else 8080
    show_default=True,
    help="Port to listen on for SSE and HTTP dashboard.",
)
@click.option(
    "--uds",
    type=str,
    default=None,
    help=(
        "Unix domain socket path. When set, the server listens on this "
        "socket instead of host:port (SSE transport only). Useful for "
        "reverse-proxy deployments that want to avoid exposing TCP."
    ),
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"], case_sensitive=False),
    default="sse",
    show_default=True,
    help="Transport type for MCP communication (stdio or sse).",
)
@click.option(
    "--project-dir",
    type=click.Path(file_okay=False, dir_okay=True, resolve_path=True, writable=True),
    default=".",
    show_default=True,
    help="Project directory. The .agent folder will be created/used here. Defaults to current directory.",
)
@click.option(
    "--admin-token",  # Renamed from admin_token_param for clarity
    "admin_token_cli",  # Variable name for the parameter
    type=str,
    default=None,
    help="Admin token for authentication. If not provided, one will be loaded from DB or generated.",
)
@click.option(
    "--debug",
    is_flag=True,
    default=os.environ.get("MCP_DEBUG", "false").lower()
    == "true",  # Default from env var
    help="Enable debug mode for the server (more verbose logging, Starlette debug pages).",
)
@click.option(
    "--no-tui",
    is_flag=True,
    default=False,
    help="Disable the terminal UI display (logs will still go to file).",
)
@click.option(
    "--advanced",
    is_flag=True,
    default=False,
    help="Enable advanced embeddings mode with larger dimension (3072) and more sophisticated code analysis.",
)
@click.option(
    "--git",
    is_flag=True,
    default=False,
    help="Enable experimental Git worktree support for parallel agent development (advanced users only).",
)
@click.option(
    "--no-index",
    is_flag=True,
    default=False,
    help="Disable automatic markdown file indexing. Allows selective manual indexing of specific content into the RAG system.",
)
def server_cmd(
    port: int,
    uds: Optional[str],
    transport: str,
    project_dir: str,
    admin_token_cli: Optional[str],
    debug: bool,
    no_tui: bool,
    advanced: bool,
    git: bool,
    no_index: bool,
):
    """
    Start the MCP Server (was the only command pre-Phase-1a).

    The server supports two embedding modes:
    - Simple mode (default): Uses text-embedding-3-large (1536 dimensions) - indexes markdown files and context
    - Advanced mode (--advanced): Uses text-embedding-3-large (3072 dimensions) - includes code analysis, task indexing

    Indexing options:
    - Default: Automatic indexing of all markdown files in project directory
    - --no-index: Disable automatic markdown indexing for selective manual control

    Note: Switching between modes will require re-indexing all content.
    """
    # Set advanced embeddings mode before other imports that might use it
    if advanced:
        from .core import config

        config.ADVANCED_EMBEDDINGS = True
        # Update the dynamic configs
        config.EMBEDDING_MODEL = config.ADVANCED_EMBEDDING_MODEL
        config.EMBEDDING_DIMENSION = config.ADVANCED_EMBEDDING_DIMENSION
        logger.info(
            "Advanced embeddings mode enabled (3072 dimensions, text-embedding-3-large, code & task indexing)"
        )
    else:
        from .core.config import SIMPLE_EMBEDDING_DIMENSION, SIMPLE_EMBEDDING_MODEL

        logger.info(
            f"Using simple embeddings mode ({SIMPLE_EMBEDDING_DIMENSION} dimensions, {SIMPLE_EMBEDDING_MODEL}, markdown & context only)"
        )

    # Initialize Git worktree support if enabled
    if git:
        try:
            from .features.worktree_integration import enable_worktree_support

            worktree_enabled = enable_worktree_support()
            if worktree_enabled:
                logger.info(
                    "🌿 Git worktree support enabled for parallel agent development"
                )
            else:
                logger.warning(
                    "❌ Git worktree support could not be enabled - check requirements"
                )
                logger.warning("   Continuing without worktree support...")
        except ImportError:
            logger.error(
                "❌ Git worktree features not available - missing dependencies"
            )
            logger.warning("   Continuing without worktree support...")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Git worktree support: {e}")
            logger.warning("   Continuing without worktree support...")
    else:
        logger.info("Git worktree support disabled (use --git to enable)")

    # Set auto-indexing configuration
    if no_index:
        from .core import config

        config.DISABLE_AUTO_INDEXING = True
        logger.info(
            "Automatic markdown indexing disabled. Use manual indexing via RAG tools for selective content."
        )
    else:
        from .core import config

        config.DISABLE_AUTO_INDEXING = False
        logger.info("Automatic markdown indexing enabled.")

    if debug:
        os.environ["MCP_DEBUG"] = (
            "true"  # Ensure env var is set for Starlette debug mode
        )
        enable_console_logging()  # Enable console logging for debug mode
        logger.info(
            "Debug mode enabled via CLI flag or MCP_DEBUG environment variable."
        )
        logger.info("Console logging enabled for debug mode.")
        # Logging level might need to be adjusted here if not already handled by config.py
        # For now, config.py sets the base level. Uvicorn also has its own log level.
    else:
        os.environ["MCP_DEBUG"] = "false"

    # Determine if the TUI should be active
    # TUI is active if console logging is disabled AND --no-tui is NOT passed AND not in debug mode
    from .core.config import (
        CONSOLE_LOGGING_ENABLED as current_console_logging,
    )  # Get updated value

    tui_active = not current_console_logging and not no_tui and not debug

    if tui_active:
        logger.info(
            "TUI display mode is active. Standard console logging is suppressed."
        )
    elif current_console_logging or debug:
        logger.info("Standard console logging is enabled (TUI display mode is off).")
        print("MCP Server starting with standard console logging...")
    else:  # Console logging is off, and TUI is also off
        logger.info(
            "Console logging and TUI display are both disabled. Check log file for server messages."
        )

    # Log the embedding mode being used
    embedding_mode_info = "advanced" if advanced else "simple"
    if advanced:
        embedding_model_info = (
            config.EMBEDDING_MODEL if "config" in locals() else "text-embedding-3-large"
        )
        embedding_dim_info = (
            config.EMBEDDING_DIMENSION if "config" in locals() else 3072
        )
    else:
        from .core.config import SIMPLE_EMBEDDING_DIMENSION, SIMPLE_EMBEDDING_MODEL

        embedding_model_info = SIMPLE_EMBEDDING_MODEL
        embedding_dim_info = SIMPLE_EMBEDDING_DIMENSION

    logger.info(
        f"Attempting to start MCP Server: Port={port}, Transport={transport}, ProjectDir='{project_dir}'"
    )
    logger.info(
        f"Embedding Mode: {embedding_mode_info} (Model: {embedding_model_info}, Dimensions: {embedding_dim_info})"
    )

    # --- TUI Display Loop (if not disabled) ---
    async def tui_display_loop(
        cli_port: int,
        cli_transport: str,
        cli_project_dir: str,
        *,
        task_status=anyio.TASK_STATUS_IGNORED,
    ):
        task_status.started()
        logger.info("TUI display loop started.")
        tui = TUIDisplay()
        initial_display = True

        # Import required modules
        from .core import globals as globals_module
        from .db.actions.agent_db import get_all_active_agents_from_db
        from .db.actions.task_db import (
            get_all_tasks_from_db,
            get_task_by_id,
            get_tasks_by_agent_id,
        )
        from datetime import datetime
        from .tui.colors import TUITheme

        # Simple tracking of server status for display
        async def get_server_status():
            try:
                return {
                    "running": globals_module.server_running,
                    "status": "Running" if globals_module.server_running else "Stopped",
                    "port": cli_port,
                }
            except Exception as e:
                logger.error(f"Error getting server status: {e}")
                return {
                    "running": globals_module.server_running,
                    "status": "Error",
                    "port": cli_port,
                }

        try:
            # Wait a moment for server initialization to complete
            await anyio.sleep(2)

            # Setup alternate screen and hide cursor for smoother display
            tui.enable_alternate_screen()
            tui.hide_cursor()

            first_draw = True

            while globals_module.server_running:
                server_status = await get_server_status()

                # Clear screen only on first draw
                if first_draw:
                    tui.clear_screen()
                    first_draw = False

                # Move to top and redraw
                tui.move_cursor(1, 1)
                current_row = tui.draw_header(clear_first=False)

                # Position cursor for status bar
                tui.move_cursor(current_row, 1)
                tui.draw_status_bar(server_status)
                current_row += 2

                # Display simplified server info
                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(TUITheme.header(" MCP Server Running"))
                current_row += 2

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(f"Project Directory: {TUITheme.info(cli_project_dir)}")
                current_row += 1

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(f"Transport: {TUITheme.info(cli_transport)}")
                current_row += 1

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(f"MCP Port: {TUITheme.info(str(cli_port))}")
                current_row += 1

                # Display admin token
                admin_token = get_admin_token_from_db(cli_project_dir)
                if admin_token:
                    tui.move_cursor(current_row, 1)
                    tui.clear_line()
                    print(f"Admin Token: {TUITheme.info(admin_token)}")
                    current_row += 1

                current_row += 2

                # Display dashboard instructions
                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(TUITheme.header(" Next Steps"))
                current_row += 2

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print("1. Open a new terminal window")
                current_row += 1

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                dashboard_path = (
                    f"{cli_project_dir}/agent_mcp/dashboard"
                    if cli_project_dir != "."
                    else "agent_mcp/dashboard"
                )
                print(f"2. Navigate to: {TUITheme.info(dashboard_path)}")
                current_row += 1

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(f"3. Run: {TUITheme.bold('npm run dev')}")
                current_row += 1

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(f"4. Open: {TUITheme.info('http://localhost:3847')}")
                current_row += 3

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(
                    TUITheme.warning(
                        "Keep this MCP server running while using the dashboard"
                    )
                )
                current_row += 2

                tui.move_cursor(current_row, 1)
                tui.clear_line()
                print(TUITheme.info("Press Ctrl+C to stop the MCP server"))
                current_row += 1

                # Clear remaining lines to prevent artifacts
                for row in range(current_row, tui.terminal_height):
                    tui.move_cursor(row, 1)
                    tui.clear_line()

                if initial_display:
                    initial_display = False

                await anyio.sleep(5)  # Refresh less frequently since display is simpler
        except anyio.get_cancelled_exc_class():
            logger.info("TUI display loop cancelled.")
        finally:
            # Cleanup the terminal
            tui.show_cursor()
            tui.disable_alternate_screen()
            tui.clear_screen()
            print("MCP Server TUI has exited.")
            logger.info("TUI display loop finished.")

    # The application_startup logic (including setting MCP_PROJECT_DIR env var,
    # DB init, admin token handling, state loading, OpenAI init, VSS check, signal handlers)
    # is now part of the Starlette app's on_startup event, triggered by create_app.

    if transport == "sse":
        # Create the Starlette application instance.
        # `application_startup` will be called by Starlette during its startup phase.
        starlette_app = create_app(
            project_dir=project_dir, admin_token_cli=admin_token_cli
        )

        # Uvicorn configuration
        # log_config=None prevents Uvicorn from overriding our logging setup from config.py
        # (Original main.py:2630)
        # When --uds is set, bind a Unix domain socket instead of a TCP
        # host:port. Useful for reverse-proxy deployments where the
        # proxy speaks to the backend over a UDS and never exposes it
        # to the network.
        _uvicorn_kwargs: dict = dict(
            log_config=None,  # Use our custom logging setup
            access_log=False,  # Disable access logs
            lifespan="on",  # Ensure Starlette's on_startup/on_shutdown are used
        )
        if uds:
            _uvicorn_kwargs["uds"] = uds
        else:
            _uvicorn_kwargs["host"] = "0.0.0.0"
            _uvicorn_kwargs["port"] = port
        uvicorn_config = uvicorn.Config(starlette_app, **_uvicorn_kwargs)
        server = uvicorn.Server(uvicorn_config)

        # Run Uvicorn server with background tasks managed by an AnyIO task group
        # This replaces the original run_server_with_background_tasks (main.py:2624)
        async def run_sse_server_with_bg_tasks():
            nonlocal server  # Allow modification if server needs to be accessed (e.g. server.should_exit)
            try:
                async with anyio.create_task_group() as tg:
                    # Start background tasks (e.g., RAG indexer)
                    # `application_startup` (called by Starlette) prepares everything.
                    # `start_background_tasks` actually launches them in the task group.
                    await start_background_tasks(tg)

                    # Start TUI display loop if enabled
                    if tui_active:
                        await tg.start(tui_display_loop, port, transport, project_dir)

                    # Start the Uvicorn server
                    logger.info(
                        f"Starting Uvicorn server for SSE transport on http://0.0.0.0:{port}"
                    )
                    logger.info(f"Dashboard available at http://localhost:{port}")
                    logger.info(
                        f"Admin token will be displayed by server startup sequence if generated/loaded."
                    )
                    logger.info("Press Ctrl+C to shut down the server gracefully.")

                    # Show standard startup messages only if TUI is not active
                    if not tui_active:
                        # Show AGENT MCP banner
                        from .tui.colors import get_responsive_agent_mcp_banner

                        print()
                        print(get_responsive_agent_mcp_banner())
                        print()
                        # When --uds is set, the server is listening on a
                        # Unix domain socket, NOT a TCP port. Logging
                        # `port {port}` here is actively misleading — a
                        # reverse-proxy operator reading the journal sees
                        # the wrong endpoint and concludes the binary
                        # ignored --uds, when in fact uvicorn was
                        # correctly configured with `uds=...` upstream
                        # (see _uvicorn_kwargs build around line 555).
                        if uds:
                            print(f"🚀 MCP Server listening on UDS {uds}")
                        else:
                            print(f"🚀 MCP Server running on port {port}")
                        print(f"📁 Project: {project_dir}")

                        # Display admin token from database
                        admin_token = get_admin_token_from_db(project_dir)
                        if admin_token:
                            print(f"🔑 Admin Token: {admin_token}")

                        print()
                        print("Next steps:")
                        dashboard_path = (
                            f"{project_dir}/agent_mcp/dashboard"
                            if project_dir != "."
                            else "agent_mcp/dashboard"
                        )
                        print(f"1. Open new terminal → cd {dashboard_path}")
                        print("2. Run: npm run dev")
                        print("3. Open: http://localhost:3847")
                        print()
                        print("Keep this server running. Press Ctrl+C to quit.")

                    await server.serve()

                    # This part is reached after server.serve() finishes (e.g., on shutdown signal)
                    logger.info(
                        "Uvicorn server has stopped. Waiting for background tasks to finalize..."
                    )
            except Exception as e:  # Catch errors during server run or task group setup
                logger.critical(
                    f"Fatal error during SSE server execution: {e}", exc_info=True
                )
                # Ensure g.server_running is false so other parts know to stop
                g.server_running = False
                # Consider re-raising or exiting if this is a critical unrecoverable error
            finally:
                logger.info("SSE server and background task group scope exited.")
                # application_shutdown is called by Starlette's on_shutdown event.

        try:
            anyio.run(run_sse_server_with_bg_tasks)
        except (
            KeyboardInterrupt
        ):  # Should be handled by signal handlers and graceful shutdown
            logger.info(
                "Keyboard interrupt received by AnyIO runner. Server should be shutting down."
            )
        except SystemExit as e:  # Catch SystemExit from application_startup
            logger.error(f"SystemExit caught: {e}. Server will not start.")
            if tui_active:
                tui = TUIDisplay()
                tui.clear_screen()
            sys.exit(e.code if isinstance(e.code, int) else 1)

    elif transport == "stdio":
        # Handle stdio transport (Original main.py:2639-2656 - arun function)
        # For stdio, we don't use Uvicorn or Starlette's HTTP capabilities.
        # We directly run the MCPLowLevelServer with stdio streams.

        async def run_stdio_server_with_bg_tasks():
            try:
                # Perform application startup manually for stdio mode as Starlette lifecycle isn't used.
                await application_startup(
                    project_dir_path_str=project_dir, admin_token_param=admin_token_cli
                )

                async with anyio.create_task_group() as tg:
                    await start_background_tasks(tg)  # Start RAG indexer etc.

                    # Start TUI display loop if enabled
                    if tui_active:
                        await tg.start(
                            tui_display_loop, 0, transport, project_dir
                        )  # Port is 0 for stdio

                    logger.info("Starting MCP server with stdio transport.")
                    logger.info("Press Ctrl+C to shut down.")

                    # Show standard startup messages only if TUI is not active
                    if not tui_active:
                        # Show AGENT MCP banner
                        from .tui.colors import get_responsive_agent_mcp_banner

                        print()
                        print(get_responsive_agent_mcp_banner())
                        print()
                        print("🚀 MCP Server running (stdio transport)")
                        print("Server is ready for AI assistant connections.")

                        # Display admin token from database
                        admin_token = get_admin_token_from_db(project_dir)
                        if admin_token:
                            print(f"🔑 Admin Token: {admin_token}")

                        print("Use Ctrl+C to quit.")

                    # Import stdio_server from mcp library
                    try:
                        from mcp.server.stdio import stdio_server
                    except ImportError:
                        logger.error(
                            "Failed to import mcp.server.stdio. Stdio transport is unavailable."
                        )
                        return

                    try:
                        async with stdio_server() as streams:
                            # mcp_app_instance is created in main_app.py and imported
                            await mcp_app_instance.run(
                                streams[0],  # input_stream
                                streams[1],  # output_stream
                                mcp_app_instance.create_initialization_options(),
                            )
                    except (
                        Exception
                    ) as e_mcp_run:  # Catch errors from mcp_app_instance.run
                        logger.error(
                            f"Error during MCP stdio server run: {e_mcp_run}",
                            exc_info=True,
                        )
                    finally:
                        logger.info("MCP stdio server run finished.")
                        # Ensure g.server_running is false to stop background tasks
                        g.server_running = False

            except Exception as e:  # Catch errors during stdio setup or task group
                logger.critical(
                    f"Fatal error during stdio server execution: {e}", exc_info=True
                )
                g.server_running = False
            finally:
                logger.info("Stdio server and background task group scope exited.")
                # Manually call application_shutdown for stdio mode
                await application_shutdown()

        try:
            anyio.run(run_stdio_server_with_bg_tasks)
        except KeyboardInterrupt:
            logger.info(
                "Keyboard interrupt received by AnyIO runner for stdio. Server should be shutting down."
            )
        except SystemExit as e:  # Catch SystemExit from application_startup
            logger.error(f"SystemExit caught: {e}. Server will not start.")
            if tui_active:
                tui = TUIDisplay()
                tui.clear_screen()
            sys.exit(e.code if isinstance(e.code, int) else 1)

    else:  # Should not happen due to click.Choice
        logger.error(f"Invalid transport type specified: {transport}")
        click.echo(
            f"Error: Invalid transport type '{transport}'. Choose 'stdio' or 'sse'.",
            err=True,
        )
        sys.exit(1)

    logger.info("MCP Server has shut down.")

    # Clear console one last time if TUI was active
    if tui_active:
        tui = TUIDisplay()
        tui.clear_screen()

    sys.exit(0)  # Explicitly exit after cleanup if not already exited by SystemExit


# --- `router` subcommand ---
# Thin wrapper around `agent_mcp.router.app.main`. The underlying app
# reads its config from `AGENT_MCP_*` env vars at module import time —
# this subcommand sets defaults for them from CLI flags before doing
# the import, so users get both an ergonomic CLI and the env-var
# escape hatch the deploy repo currently uses.
@cli.command("router", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option(
    "--port",
    type=int,
    default=lambda: int(os.environ.get("AGENT_MCP_ROUTER_PORT", "1337")),
    show_default="1337 (or $AGENT_MCP_ROUTER_PORT)",
    help="Port to listen on for the URL-keyed router.",
)
@click.option(
    "--projects-file",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=lambda: os.environ.get(
        "AGENT_MCP_PROJECTS_FILE",
        str(
            Path(
                os.environ.get(
                    "XDG_CONFIG_HOME", str(Path.home() / ".config")
                )
            )
            / "agent-mcp"
            / "projects.local.json"
        ),
    ),
    show_default="~/.config/agent-mcp/projects.local.json",
    help="JSON file mapping project name → workspace path.",
)
@click.option(
    "--sock-dir",
    type=click.Path(file_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_SOCK_DIR"),
    show_default="$AGENT_MCP_SOCK_DIR",
    help="Directory containing per-project Unix-domain backend sockets.",
)
@click.option(
    "--dashboard-dir",
    type=click.Path(file_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_DASHBOARD_DIR"),
    show_default="$AGENT_MCP_DASHBOARD_DIR",
    help="Directory holding the Next.js static dashboard export.",
)
@click.option(
    "--external-url",
    type=str,
    default=lambda: os.environ.get("AGENT_MCP_EXTERNAL_URL"),
    show_default="$AGENT_MCP_EXTERNAL_URL",
    help="Base URL the router reachable at (used in copy-paste wiring snippets).",
)
@click.option(
    "--idle-sec",
    type=int,
    default=lambda: int(os.environ.get("AGENT_MCP_IDLE_SEC", str(4 * 60 * 60))),
    show_default="14400 (4h)",
    help="Idle seconds before stopping an inactive backend.",
)
@click.option(
    "--installer-template",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_INSTALLER_TEMPLATE"),
    show_default="packaged installer.sh.in (or $AGENT_MCP_INSTALLER_TEMPLATE)",
    help="Path to the installer.sh.in template (env override).",
)
@click.option(
    "--readme-html",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_README_HTML") or None,
    show_default="(none)",
    help="Optional README rendered to HTML, embedded in the index page.",
)
@click.option(
    "--asset-prefix",
    "asset_prefix",
    type=str,
    default=lambda: os.environ.get("AGENT_MCP_ASSET_PREFIX") or None,
    show_default="$AGENT_MCP_ASSET_PREFIX (default: /agent-mcp/__dashboard)",
    help=(
        "Runtime URL prefix substituted into the dashboard's "
        "sentinel-marked asset URLs on serve (Phase 4). Set this to "
        "match the path the dashboard is mounted at by your reverse "
        "proxy. Default ``/agent-mcp/__dashboard`` matches the "
        "router's own dashboard route table. One build artifact "
        "serves any prefix — no rebuild needed."
    ),
)
@click.option(
    "--single-tenant",
    "single_tenant_name",
    type=str,
    default=lambda: os.environ.get("AGENT_MCP_SINGLE_TENANT_NAME") or None,
    show_default="$AGENT_MCP_SINGLE_TENANT_NAME (else multi-tenant)",
    help=(
        "Run the router in single-tenant mode for the named project. "
        "Disables __create / __unregister / __rename (410); URLs naming "
        "any other project are 302-redirected to the configured one "
        "(W1; ADR-0008). Pair with --single-workspace."
    ),
)
@click.option(
    "--single-workspace",
    "single_tenant_workspace",
    type=click.Path(file_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_SINGLE_TENANT_WORKSPACE") or None,
    show_default="$AGENT_MCP_SINGLE_TENANT_WORKSPACE",
    help=(
        "Workspace path for the single-tenant project. The home-manager "
        "module's ExecStartPre seeds projects.local.json with this entry "
        "before the router starts, so the router can still resolve the "
        "single project's UDS via its registry lookup."
    ),
)
def router_cmd(
    port: int,
    projects_file: str,
    sock_dir: Optional[str],
    dashboard_dir: Optional[str],
    external_url: Optional[str],
    idle_sec: int,
    installer_template: Optional[str],
    readme_html: str,
    asset_prefix: Optional[str],
    single_tenant_name: Optional[str],
    single_tenant_workspace: Optional[str],
) -> None:
    """Run the always-on URL-keyed HTTP router.

    The router proxies /agent-mcp/<name>/* to the per-project backend
    over Unix-domain sockets and serves the shared Next.js dashboard
    + index page at /agent-mcp/.
    """
    # Promote CLI flags to env vars so the app module's import-time
    # reads pick them up. The deploy repo still sets these env vars
    # directly via systemd; both paths converge here.
    os.environ["AGENT_MCP_ROUTER_PORT"] = str(port)
    os.environ["AGENT_MCP_PROJECTS_FILE"] = projects_file
    if sock_dir:
        os.environ["AGENT_MCP_SOCK_DIR"] = sock_dir
    if dashboard_dir:
        os.environ["AGENT_MCP_DASHBOARD_DIR"] = dashboard_dir
    if external_url:
        os.environ["AGENT_MCP_EXTERNAL_URL"] = external_url
    os.environ["AGENT_MCP_IDLE_SEC"] = str(idle_sec)
    if installer_template:
        os.environ["AGENT_MCP_INSTALLER_TEMPLATE"] = installer_template
    if readme_html:
        os.environ["AGENT_MCP_README_HTML"] = readme_html
    if asset_prefix is not None:
        os.environ["AGENT_MCP_ASSET_PREFIX"] = asset_prefix

    # Required env vars without defaults: surface a clean error
    # rather than a KeyError stack trace deep inside the app.
    required = {
        "AGENT_MCP_SOCK_DIR": "--sock-dir",
        "AGENT_MCP_DASHBOARD_DIR": "--dashboard-dir",
        "AGENT_MCP_EXTERNAL_URL": "--external-url",
    }
    missing = [
        f"{flag} (or ${env})"
        for env, flag in required.items()
        if not os.environ.get(env)
    ]
    if missing:
        raise click.UsageError(
            "router subcommand requires: " + ", ".join(missing)
        )

    # --single-tenant / --single-workspace must come as a pair (or
    # not at all). Catch the lopsided invocation here so the operator
    # gets a clean error rather than a router that's half-toggled.
    if (single_tenant_name is None) != (single_tenant_workspace is None):
        raise click.UsageError(
            "--single-tenant and --single-workspace must be passed together"
        )

    # Lazy import — the router module reads env at top level, so
    # importing it before the os.environ assignments above would
    # bind to stale (likely missing) values.
    from .router.app import make_app
    from aiohttp import web

    app = make_app(
        single_tenant_name=single_tenant_name,
        single_tenant_workspace=single_tenant_workspace,
    )
    # Same env-override-on-bind-host pattern as router.app.main —
    # used by the VM tests' module so qemu hostfwd can route in.
    host = os.environ.get("AGENT_MCP_ROUTER_HOST", "127.0.0.1")
    # `shutdown_timeout=3.0` matches `router.app.main()` — see the
    # comment there. Capping the aiohttp drain window paired with
    # the `_drain_proxy_tasks` on_shutdown hook keeps the SIGTERM-
    # to-exit window inside systemd's `TimeoutStopSec=15s`, fixing
    # the 90 s deploy outage caused by long-lived MCP proxy streams.
    web.run_app(app, host=host, port=port, shutdown_timeout=3.0)


# --- Backward-compatibility shim ---
# Pre-Phase-1a invocations looked like:
#   python -m agent_mcp.cli --transport sse --uds /run/.../backend.sock \
#                           --project-dir /path --no-tui
# i.e. top-level flags, no subcommand. The deploy repo's wrapper
# script (`agent-mcp-backend`) used this exact shape. We keep one
# release of compatibility by sniffing argv and rerouting through
# the new `server` subcommand with a DeprecationWarning. Remove in
# a future PR once the deploy repo has switched over.
_TOP_LEVEL_FLAGS_THAT_NOW_BELONG_TO_SERVER = {
    "--port",
    "--uds",
    "--transport",
    "--project-dir",
    "--admin-token",
    "--debug",
    "--no-tui",
    "--advanced",
    "--git",
    "--no-index",
}


def _looks_like_legacy_top_level_invocation(argv: list[str]) -> bool:
    """True iff argv[1] is a flag that used to live on the top-level
    command but now lives under `server`. Subcommands like `server`
    and `router` never start with `-`, so this is unambiguous."""
    if len(argv) < 2:
        return False
    first = argv[1]
    if not first.startswith("-"):
        return False
    # Accept both `--flag` and `--flag=value`.
    head = first.split("=", 1)[0]
    return head in _TOP_LEVEL_FLAGS_THAT_NOW_BELONG_TO_SERVER


# --- `backup` subcommand ---
# Per item 12 of the 2026-06-02 database review. The previous
# project_context-only JSON dump was the only backup surface; this
# is a full-DB online backup via sqlite3.Connection.backup(), which
# is safe under WAL (doesn't block writers).
@cli.command("backup", context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument(
    "project_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
)
@click.argument("output_path", type=click.Path(dir_okay=False))
@click.option(
    "--force/--no-force",
    default=False,
    help=(
        "Overwrite OUTPUT_PATH if it already exists. Without this "
        "flag, the command refuses to clobber an existing file."
    ),
)
def backup_cmd(project_dir: str, output_path: str, force: bool) -> None:
    """Back up a project's SQLite database to OUTPUT_PATH.

    Uses sqlite3.Connection.backup() — the canonical online backup
    API. Safe to run while the server is live; readers and writers
    keep going.

    PROJECT_DIR is the directory containing `.agent/mcp_state.db`
    (the same path you'd pass to `agent-mcp server --project-dir`).
    """
    src_path = Path(project_dir).resolve() / ".agent" / "mcp_state.db"
    dst_path = Path(output_path)

    if not src_path.exists():
        click.echo(
            f"Error: database not found at {src_path}",
            err=True,
        )
        sys.exit(1)

    if dst_path.exists() and not force:
        click.echo(
            f"Error: output file {dst_path} already exists; "
            f"pass --force to overwrite",
            err=True,
        )
        sys.exit(1)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    # If --force, remove the existing target so the backup writes a
    # fresh DB rather than appending to an unrelated sqlite file.
    if dst_path.exists() and force:
        dst_path.unlink()

    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dst_path))
    try:
        # progress callback gets (status, remaining, total); we don't
        # need to surface it interactively for now but the API leaves
        # the hook available for a future --progress flag.
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    click.echo(f"Backup complete: {dst_path}")


def main() -> None:
    """Public entry point.

    Handles the backward-compat rewrite (legacy top-level flags →
    `server` subcommand) before handing off to the click group.
    """
    if _looks_like_legacy_top_level_invocation(sys.argv):
        warnings.warn(
            "Top-level flags will be removed in a future release; "
            "use 'agent-mcp server …' (e.g. "
            "'agent-mcp server --transport sse …') instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        sys.argv = [sys.argv[0], "server", *sys.argv[1:]]
    cli()


# This allows running `python -m agent_mcp.cli --port ...`
if __name__ == "__main__":
    main()
