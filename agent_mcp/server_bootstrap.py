"""Server bootstrap — single ordered boot path for the MCP backend.

Before this module the boot ordering was scattered across three files:

* ``agent_mcp/cli.py`` (a click adapter that also walked ``.env``,
  branched on transport, owned the TUI display loop, and called the
  Starlette runners).
* ``agent_mcp/app/main_app.py`` (Starlette wiring).
* ``agent_mcp/app/server_lifecycle.py`` (lifespan handlers).

Answering "where does the server start?" required reading three files,
and adding a new boot dependency meant editing two of them. This
module is the single home for the orchestration step: take a
``ServerConfig``, return a Starlette app + a teardown callable, and
let the CLI stay a thin adapter.

What stays elsewhere (deliberate):

* Click decorators / option parsing — ``cli.py``. The CLI is still the
  user-facing surface; only the orchestration moves.
* The Starlette app factory (``create_app``) — ``app/main_app.py``.
  We *call* it from here; we don't absorb it.
* Lifespan handlers (``application_startup``, ``application_shutdown``,
  ``start_background_tasks``) — ``app/server_lifecycle.py``. Starlette
  calls those during request handling via the lifespan context
  manager wired by ``create_app``; the bootstrap module never invokes
  them directly for sse transport (they're driven by the lifespan
  contract). Stdio transport doesn't have a Starlette lifespan, so
  the stdio runner does call them by hand — that's the historical
  shape and the lifespan-loads-active-agents test guards it.
* The TUI display loop — ``tui/runtime.py`` (its own module post-PR-E).
* The ``router`` + ``backup`` subcommands — ``cli.py``. They're
  separate concerns (the router is its own aiohttp app; the backup
  command is a one-shot SQLite copy) and the architecture review
  scopes this PR to the ``server`` boot path.

Reading order:

  1. ``ServerConfig`` — frozen dataclass; describes "what to boot".
  2. ``load_project_dotenv`` — discovery step; populates env before
     any module reads it.
  3. ``apply_runtime_flags`` — translates the ``advanced`` / ``git`` /
     ``no_index`` / ``debug`` flags into the side effects the rest of
     the codebase reads (``core.config`` knobs, ``MCP_DEBUG`` env var,
     worktree feature toggle).
  4. ``bootstrap_server`` — builds the Starlette app (sse) or returns
     ``(None, teardown)`` (stdio).
  5. ``run_server`` — async runners (sse uvicorn + bg tasks, stdio
     mcp pump). Called by the CLI subcommand callback after
     ``ServerConfig.from_cli_args``.

The teardown returned by ``bootstrap_server`` is idempotent — the CLI
runner may invoke it on both the normal-exit and SystemExit paths and
the test contract pins that calling it twice does not raise.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from dotenv import dotenv_values, load_dotenv

from .core.config import logger


# --- ServerConfig ----------------------------------------------------

# The transport choices match the click ``--transport`` option; keep
# this tuple in lockstep with the click.Choice list. Surface as a
# constant so tests + the click decorator can both reference it.
VALID_TRANSPORTS: Tuple[str, ...] = ("stdio", "sse")


@dataclass(frozen=True)
class ServerConfig:
    """All settings needed to boot the MCP backend, derived from the
    click-decoded CLI options.

    Frozen so a stray ``cfg.port = 9000`` after the bootstrap runs is
    caught at write time rather than deep in a background task that
    captured the value at boot. Field set mirrors the legacy
    ``server_cmd`` callback signature one-for-one — if a future PR
    needs to thread a new option through, add it here and the CLI
    keeps being a thin pass-through.
    """

    transport: str
    port: int
    project_dir: str
    uds: Optional[str] = None
    # Phase 2 Wave 1b: renamed from admin_token_*. The legacy attribute
    # names are NOT preserved on this frozen dataclass — internal
    # callers were updated in the same PR. External constructors get
    # the migration via the ``admin_token_*`` kwargs on ``from_cli_args``
    # below.
    system_token_cli: Optional[str] = None
    system_token_out_path: Optional[str] = None
    system_token_out_format: str = "raw"
    system_token_in_path: Optional[str] = None
    system_token_log: bool = False
    # retire-system-token Wave 1: path to the per-project HMAC key
    # file the router uses to sign the forwarding header. The backend
    # reads the raw bytes at boot and stamps them onto
    # ``g.forwarding_hmac_key`` so ``AuthHeaderMiddleware`` can verify
    # the header. When unset, the forwarding-header path is dormant
    # (Wave 2/3 transitional state).
    forwarding_hmac_in_path: Optional[str] = None
    debug: bool = False
    no_tui: bool = False
    advanced: bool = False
    git: bool = False
    no_index: bool = False

    def __post_init__(self) -> None:
        if self.transport not in VALID_TRANSPORTS:
            raise ValueError(
                f"transport must be one of {VALID_TRANSPORTS!r}, got "
                f"{self.transport!r}"
            )

    @classmethod
    def from_cli_args(
        cls,
        *,
        port: int,
        uds: Optional[str],
        transport: str,
        project_dir: str,
        debug: bool,
        no_tui: bool,
        advanced: bool,
        git: bool,
        no_index: bool,
        system_token_cli: Optional[str] = None,
        system_token_out_path: Optional[str] = None,
        system_token_out_format: str = "raw",
        system_token_in_path: Optional[str] = None,
        system_token_log: bool = False,
        forwarding_hmac_in_path: Optional[str] = None,
        # Phase 2 Wave 1b: legacy ``admin_token_*`` aliases kept for
        # one release so external callers (deploy scripts, third-party
        # integrations) keep working. New name wins on collision.
        admin_token_cli: Optional[str] = None,
        admin_token_out_path: Optional[str] = None,
        admin_token_out_format: Optional[str] = None,
        admin_token_in_path: Optional[str] = None,
        admin_token_log: Optional[bool] = None,
    ) -> "ServerConfig":
        """Build a config from the keyword set the click ``server``
        subcommand passes to its callback.

        Normalises ``project_dir`` to an absolute path so background
        tasks that fire after a ``chdir`` (the deploy repo's pre-start
        hook does this) still resolve the right ``.agent`` directory.
        Validation happens via ``__post_init__`` so callers always
        catch bad input synchronously.

        Accepts both ``system_token_*`` (canonical) and ``admin_token_*``
        (deprecated alias) kwargs; the canonical name wins when both
        are supplied.
        """
        if system_token_cli is None and admin_token_cli is not None:
            system_token_cli = admin_token_cli
        if system_token_out_path is None and admin_token_out_path is not None:
            system_token_out_path = admin_token_out_path
        if system_token_out_format == "raw" and admin_token_out_format is not None:
            system_token_out_format = admin_token_out_format
        if system_token_in_path is None and admin_token_in_path is not None:
            system_token_in_path = admin_token_in_path
        if system_token_log is False and admin_token_log is True:
            system_token_log = True

        resolved_project = str(Path(project_dir).resolve())
        return cls(
            transport=transport.lower(),
            port=int(port),
            project_dir=resolved_project,
            uds=uds,
            system_token_cli=system_token_cli,
            system_token_out_path=system_token_out_path,
            system_token_out_format=system_token_out_format,
            system_token_in_path=system_token_in_path,
            system_token_log=bool(system_token_log),
            forwarding_hmac_in_path=forwarding_hmac_in_path,
            debug=bool(debug),
            no_tui=bool(no_tui),
            advanced=bool(advanced),
            git=bool(git),
            no_index=bool(no_index),
        )


# --- .env discovery --------------------------------------------------

# Legacy ``cli.py`` walked up to 3 parent levels from its own
# location, dotenv-loading each ``.env`` it found and re-exporting
# every variable into ``os.environ``. Pulled out here so the test
# suite can drive it with a ``search_from`` seam rather than having
# to relocate ``cli.py`` itself.

_DEFAULT_ENV_PARENT_LEVELS = 3


def load_project_dotenv(
    *,
    search_from: Optional[Path] = None,
    max_parents: int = _DEFAULT_ENV_PARENT_LEVELS,
) -> None:
    """Walk parents of ``search_from`` looking for ``.env`` files.

    Mirrors the legacy ``cli.py`` import-time block: scan up to
    ``max_parents`` levels, ``dotenv_values`` each match, write each
    key into ``os.environ``. Finally call plain ``load_dotenv()`` so
    the standard discovery (cwd) also runs — some deploy setups don't
    co-locate ``.env`` with the source tree.

    ``search_from`` defaults to this module's directory (matching
    legacy behaviour); tests inject a fixture path.
    """
    base = (search_from or Path(__file__).resolve().parent).resolve()
    # Range is inclusive of "this directory" — match the legacy
    # ``parent_level in range(3)`` exactly so an .env one level up
    # from the package still wins.
    for parent_level in range(max_parents + 1):
        env_path = (base / ("../" * parent_level) / ".env").resolve()
        if env_path.exists():
            try:
                values = dotenv_values(str(env_path))
            except Exception as exc:  # pragma: no cover - dotenv parse errors
                logger.warning(
                    "load_project_dotenv: failed to parse %s (%s); skipping.",
                    env_path,
                    exc,
                )
                continue
            for key, value in values.items():
                if value is not None:
                    os.environ[key] = value
            logger.info("load_project_dotenv: loaded %s", env_path)
            break

    # Fall back to the default discovery (cwd) for setups that don't
    # co-locate .env with the package source tree.
    load_dotenv()


# --- runtime-flag → side-effect translation --------------------------


def apply_runtime_flags(config: ServerConfig) -> None:
    """Translate the boolean flags on ``config`` into the global side
    effects the rest of the codebase reads.

    Specifically:

    * ``--advanced`` flips ``core.config.ADVANCED_EMBEDDINGS`` and
      retargets ``EMBEDDING_MODEL`` / ``EMBEDDING_DIMENSION``. Done
      *before* any RAG indexer module touches them.
    * ``--no-index`` flips ``core.config.DISABLE_AUTO_INDEXING``.
    * ``--debug`` exports ``MCP_DEBUG=true|false`` — Starlette reads
      this at app construction time inside ``create_app``.
    * ``--git`` activates the worktree integration feature if the
      module is importable. Same try/except shape as the legacy CLI
      so deploys without the optional dep still boot.

    Idempotent: calling it twice with the same config is a no-op
    (the writes are deterministic).
    """
    from .core import config as core_config

    # Embeddings
    if config.advanced:
        core_config.ADVANCED_EMBEDDINGS = True
        core_config.EMBEDDING_MODEL = core_config.ADVANCED_EMBEDDING_MODEL
        core_config.EMBEDDING_DIMENSION = core_config.ADVANCED_EMBEDDING_DIMENSION
        logger.info(
            "Advanced embeddings mode enabled (%d dimensions, %s).",
            core_config.ADVANCED_EMBEDDING_DIMENSION,
            core_config.ADVANCED_EMBEDDING_MODEL,
        )
    else:
        # Default is the simple model; only log so operators see which
        # mode is active.
        logger.info(
            "Using simple embeddings mode (%d dimensions, %s).",
            core_config.SIMPLE_EMBEDDING_DIMENSION,
            core_config.SIMPLE_EMBEDDING_MODEL,
        )

    # Auto-indexing
    core_config.DISABLE_AUTO_INDEXING = bool(config.no_index)
    if config.no_index:
        logger.info(
            "Automatic markdown indexing disabled (--no-index). Use the "
            "RAG tools for selective manual indexing."
        )
    else:
        logger.info("Automatic markdown indexing enabled.")

    # Debug → env var. The Starlette app reads this on construction;
    # flipping it after create_app is a no-op so we must export it
    # before bootstrap_server's create_app call.
    os.environ["MCP_DEBUG"] = "true" if config.debug else "false"
    if config.debug:
        from .core.config import enable_console_logging

        enable_console_logging()
        logger.info("Debug mode enabled via CLI flag.")

    # Optional worktree feature toggle. The legacy CLI wrapped this in
    # an aggressive try/except so a missing optional dep doesn't break
    # boot — preserve that shape.
    if config.git:
        try:
            from .features.worktree_integration import enable_worktree_support

            if enable_worktree_support():
                logger.info(
                    "Git worktree support enabled for parallel agent development."
                )
            else:
                logger.warning(
                    "Git worktree support could not be enabled — check "
                    "requirements. Continuing without."
                )
        except ImportError:
            logger.error(
                "Git worktree features not available (missing dependencies). "
                "Continuing without worktree support."
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Failed to initialize Git worktree support: %s. Continuing "
                "without.",
                exc,
            )
    else:
        logger.info("Git worktree support disabled (use --git to enable).")


# --- admin-token DB helper (read-only) -------------------------------


def get_system_token_from_db(project_dir: str) -> Optional[str]:
    """Read the persisted system token out of the project DB.

    Phase 2 Wave 1b rename of ``get_admin_token_from_db``. Reads the
    canonical ``config_system_token`` row first, falling back to the
    legacy ``config_admin_token`` row so a fresh-after-upgrade boot
    (before ``application_startup`` migrates the row) still surfaces
    the right value to the TUI / startup banner.

    Used by the TUI display loop + the startup banner to print the
    actual token a freshly booted server is using. Returns ``None`` if
    the DB is absent or both rows are missing — both are normal on first
    boot, before ``application_startup`` has written the token.

    Lives here (rather than in ``cli.py``) so the TUI runtime can
    import it without pulling the click adapter as a side effect.
    """
    try:
        db_path = Path(project_dir).resolve() / ".agent" / "mcp_state.db"
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            for key in ("config_system_token", "config_admin_token"):
                cursor.execute(
                    "SELECT value FROM project_context WHERE context_key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if row and row["value"]:
                    try:
                        token = json.loads(row["value"])
                        if isinstance(token, str) and token:
                            return token
                    except json.JSONDecodeError:
                        continue
        finally:
            conn.close()
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Error reading system token from database: %s", exc)
        return None


# Phase 2 Wave 1b: legacy alias kept for one release so external
# callers (Nix module, third-party tools) keep importing the old name.
get_admin_token_from_db = get_system_token_from_db


# --- forwarding-header HMAC key loader -------------------------------


def _load_forwarding_hmac_key(path: Optional[str]) -> None:
    """Read the per-project HMAC key file and stamp it onto
    ``g.forwarding_hmac_key``.

    Called once from ``bootstrap_server`` before the app is built.
    Used by ``AuthHeaderMiddleware`` to verify the
    ``X-Agent-MCP-Forwarded-Operator`` header the router (Wave 2)
    will attach to operator-cookie requests.

    Behaviour:
      * ``path is None`` — clear the global (None). The middleware
        treats the forwarding-header path as dormant; per-agent
        bearer tokens are the only working auth.
      * ``path`` set, file readable — read raw bytes, stamp on the
        global. Empty / whitespace-only files leave the global at
        None and log a warning (an empty HMAC key is the worst
        possible value — all signatures would verify with the empty
        key trivially because hmac.compare_digest of two empty
        strings returns True if both empty, etc.).
      * ``path`` set, file unreadable — log error, leave the global
        at None. Wave 1 is transitional and an unreadable key
        file should not crash boot.

    Wave 3 will tighten this contract once the launcher reliably
    writes the file at spawn.
    """
    from .core import globals as g

    if not path:
        g.forwarding_hmac_key = None
        return
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        logger.error(
            "Failed to read forwarding-hmac key file %s: %s. "
            "Forwarding-header auth will be dormant.",
            path,
            exc,
        )
        g.forwarding_hmac_key = None
        return
    # Allow the file to carry a trailing newline (text-editor friendly)
    # without weakening the secret. Treat all-whitespace as empty.
    data = data.strip()
    if not data:
        logger.warning(
            "Forwarding-hmac key file %s is empty after strip(); "
            "forwarding-header auth stays dormant.",
            path,
        )
        g.forwarding_hmac_key = None
        return
    g.forwarding_hmac_key = data
    logger.info(
        "Loaded forwarding-header HMAC key from %s (%d bytes).",
        path,
        len(data),
    )


# --- bootstrap_server -------------------------------------------------


def bootstrap_server(
    config: ServerConfig,
) -> Tuple[Optional["Starlette"], Callable[[], None]]:  # noqa: F821
    """Build the Starlette app + return a teardown callable.

    For ``transport='sse'`` this calls ``create_app`` from
    ``app/main_app.py`` (which wires the Starlette lifespan + mounts
    the routes). For ``transport='stdio'`` there's no Starlette app —
    the stdio runner handles the mcp lowlevel server directly — so we
    return ``(None, teardown)``.

    Side effects (in order):

      1. ``apply_runtime_flags(config)`` — flips ``core.config``
         knobs + the ``MCP_DEBUG`` env var.
      2. (sse only) ``create_app(project_dir, admin_token_cli)``
         constructs the Starlette app. Starlette's lifespan kicks off
         ``application_startup`` on first request — we DON'T pre-run
         it here, because lifespan ordering is the contract that the
         lifespan-loads-active-agents test pins.

    The teardown callable:

      * Marks ``g.server_running = False`` (so any spinning bg loops
        notice and exit).
      * Idempotent — calling it twice is a no-op; the CLI runner may
        invoke it on both the normal-exit and the SystemExit paths.
    """
    from starlette.applications import Starlette  # noqa: F401 — type only

    apply_runtime_flags(config)

    # retire-system-token Wave 1: load the forwarding-header HMAC key
    # before ``create_app`` so the middleware sees a populated
    # ``g.forwarding_hmac_key`` on the very first request. When the
    # flag isn't set, leave the global at its default ``None`` so the
    # middleware's dormant-fallback path stays in effect (per-agent
    # bearers are the only working auth).
    _load_forwarding_hmac_key(config.forwarding_hmac_in_path)

    logger.info(
        "bootstrap_server: transport=%s port=%d project_dir=%s",
        config.transport,
        config.port,
        config.project_dir,
    )

    # Imported here (rather than at module top) because ``create_app``
    # pulls in the Starlette middleware stack + the MCP lowlevel
    # server — heavy imports we don't want a stdio caller to pay for.
    app: Optional[Any] = None
    if config.transport == "sse":
        from .app.main_app import create_app

        app = create_app(
            project_dir=config.project_dir,
            system_token_cli=config.system_token_cli,
            system_token_out_path=config.system_token_out_path,
            system_token_out_format=config.system_token_out_format,
            system_token_in_path=config.system_token_in_path,
            system_token_log=config.system_token_log,
        )

    teardown_state = {"called": False}

    def teardown() -> None:
        if teardown_state["called"]:
            return
        teardown_state["called"] = True
        from .core import globals as g

        g.server_running = False
        logger.info("bootstrap_server.teardown: server_running flag cleared.")

    return app, teardown


# --- run_server (orchestration entry from cli.py) --------------------


def run_server(config: ServerConfig) -> None:
    """Run the server end-to-end.

    Called by the click ``server`` subcommand after building the
    config. Branches on ``config.transport``:

    * ``sse``: build the Starlette app via ``bootstrap_server``, wrap
      it in uvicorn (TCP or UDS), and run an anyio task group that
      hosts uvicorn + the background tasks + (optionally) the TUI
      display loop.
    * ``stdio``: don't build a Starlette app; instead call
      ``application_startup`` directly, spawn the bg tasks, and run
      the ``mcp.server.stdio.stdio_server`` pump.

    Both branches share the SystemExit / KeyboardInterrupt handling
    + the teardown idempotency contract.
    """
    import anyio

    app, teardown = bootstrap_server(config)

    # TUI is active iff console logging stayed off AND --no-tui wasn't
    # passed AND debug mode is off (debug forces console logs so the
    # operator sees the stack traces).
    from .core.config import CONSOLE_LOGGING_ENABLED as current_console_logging

    tui_active = (
        not current_console_logging and not config.no_tui and not config.debug
    )

    if tui_active:
        logger.info(
            "TUI display mode is active. Standard console logging is suppressed."
        )
    elif current_console_logging or config.debug:
        logger.info("Standard console logging is enabled (TUI display mode is off).")
        print("MCP Server starting with standard console logging...")
    else:
        logger.info(
            "Console logging and TUI display are both disabled. Check log file."
        )

    try:
        if config.transport == "sse":
            assert app is not None
            anyio.run(_run_sse, config, app, tui_active)
        elif config.transport == "stdio":
            anyio.run(_run_stdio, config, tui_active)
        else:  # pragma: no cover - guarded by ServerConfig validation
            raise RuntimeError(f"unreachable transport: {config.transport!r}")
    except KeyboardInterrupt:
        logger.info(
            "Keyboard interrupt received. Server should be shutting down."
        )
    except SystemExit as exc:
        logger.error("SystemExit caught: %s. Server will not start.", exc)
        if tui_active:
            _reset_tui()
        sys.exit(exc.code if isinstance(exc.code, int) else 1)
    finally:
        teardown()

    logger.info("MCP Server has shut down.")
    if tui_active:
        _reset_tui()


# --- private runners --------------------------------------------------


async def _run_sse(config: ServerConfig, app: Any, tui_active: bool) -> None:
    """Run uvicorn + the background tasks under one anyio task group.

    Mirrors the legacy ``run_sse_server_with_bg_tasks`` in ``cli.py``:
    start the background tasks, optionally start the TUI display loop,
    then ``server.serve()`` until shutdown. The lifespan context
    manager wired in ``create_app`` handles ``application_startup`` /
    ``application_shutdown``.
    """
    import anyio
    import uvicorn

    from .app.server_lifecycle import start_background_tasks
    from .core import globals as g

    # uvicorn binding — TCP vs UDS. Misleading "port" logging on a UDS
    # build was a 2025 incident; this code path preserves the post-fix
    # logging shape (log the UDS path, not a port number, when --uds
    # is set).
    uvicorn_kwargs: dict[str, Any] = dict(
        log_config=None,  # keep our logging setup
        access_log=False,
        lifespan="on",
    )
    if config.uds:
        uvicorn_kwargs["uds"] = config.uds
    else:
        uvicorn_kwargs["host"] = "0.0.0.0"
        uvicorn_kwargs["port"] = config.port
    uvicorn_config = uvicorn.Config(app, **uvicorn_kwargs)
    server = uvicorn.Server(uvicorn_config)

    try:
        async with anyio.create_task_group() as tg:
            await start_background_tasks(tg)

            if tui_active:
                from .tui.runtime import tui_display_loop

                await tg.start(
                    tui_display_loop, config.port, config.transport, config.project_dir
                )

            logger.info(
                "Starting Uvicorn server for SSE transport on %s.",
                f"uds {config.uds}" if config.uds else f"http://0.0.0.0:{config.port}",
            )
            logger.info("Press Ctrl+C to shut down the server gracefully.")

            if not tui_active:
                _print_startup_banner(config)

            await server.serve()
            logger.info(
                "Uvicorn server has stopped. Waiting for background tasks to finalise."
            )
    except Exception as exc:
        logger.critical("Fatal error during SSE server execution: %s", exc, exc_info=True)
        g.server_running = False
    finally:
        logger.info("SSE server and background task group scope exited.")


async def _run_stdio(config: ServerConfig, tui_active: bool) -> None:
    """Run the MCP stdio pump under an anyio task group.

    Stdio doesn't have a Starlette lifespan, so we call
    ``application_startup`` + ``application_shutdown`` by hand here.
    This matches the legacy CLI's stdio branch and is the contract the
    lifespan-loads-active-agents test pins for that path.
    """
    import anyio

    from .app.main_app import mcp_app_instance
    from .app.server_lifecycle import (
        application_shutdown,
        application_startup,
        start_background_tasks,
    )
    from .core import globals as g

    try:
        await application_startup(
            project_dir_path_str=config.project_dir,
            system_token_param=config.system_token_cli,
            system_token_out_path=config.system_token_out_path,
            system_token_out_format=config.system_token_out_format,
            system_token_in_path=config.system_token_in_path,
            system_token_log=config.system_token_log,
        )

        async with anyio.create_task_group() as tg:
            await start_background_tasks(tg)

            if tui_active:
                from .tui.runtime import tui_display_loop

                # Port=0 for stdio — there isn't one.
                await tg.start(tui_display_loop, 0, config.transport, config.project_dir)

            logger.info("Starting MCP server with stdio transport.")
            logger.info("Press Ctrl+C to shut down.")

            if not tui_active:
                _print_startup_banner(config)

            try:
                from mcp.server.stdio import stdio_server
            except ImportError:
                logger.error(
                    "Failed to import mcp.server.stdio. Stdio transport unavailable."
                )
                return

            try:
                async with stdio_server() as streams:
                    await mcp_app_instance.run(
                        streams[0],
                        streams[1],
                        mcp_app_instance.create_initialization_options(),
                    )
            except Exception as exc:
                logger.error(
                    "Error during MCP stdio server run: %s", exc, exc_info=True
                )
            finally:
                logger.info("MCP stdio server run finished.")
                g.server_running = False
    except Exception as exc:
        logger.critical(
            "Fatal error during stdio server execution: %s", exc, exc_info=True
        )
        g.server_running = False
    finally:
        logger.info("Stdio server and background task group scope exited.")
        await application_shutdown()


# --- startup-banner helpers (printed when TUI is off) ----------------


def _print_startup_banner(config: ServerConfig) -> None:
    """Print the "MCP Server running" banner + next-steps block.

    Shown when the TUI display loop is off — operators running with
    ``--no-tui`` or ``--debug`` still want a one-shot summary of where
    the server is listening + how to reach the dashboard.
    """
    from .tui.colors import get_responsive_agent_mcp_banner

    print()
    print(get_responsive_agent_mcp_banner())
    print()
    if config.transport == "stdio":
        print("🚀 MCP Server running (stdio transport)")
        print("Server is ready for AI assistant connections.")
    elif config.uds:
        print(f"🚀 MCP Server listening on UDS {config.uds}")
        print(f"📁 Project: {config.project_dir}")
    else:
        print(f"🚀 MCP Server running on port {config.port}")
        print(f"📁 Project: {config.project_dir}")

    # The startup banner only surfaces the admin token when the
    # operator opted in via --admin-token-log. Default is silent —
    # the dashboard's tokens view, --admin-token-out, or the TUI
    # (which the operator is actively staring at) are the supported
    # surfaces. Same gate as the application_startup log line.
    if config.system_token_log:
        admin_token = get_admin_token_from_db(config.project_dir)
        if admin_token:
            print(f"🔑 Admin Token: {admin_token}")

    print()
    if config.transport != "stdio":
        print("Next steps:")
        dashboard_path = (
            f"{config.project_dir}/agent_mcp/dashboard"
            if config.project_dir != "."
            else "agent_mcp/dashboard"
        )
        print(f"1. Open new terminal → cd {dashboard_path}")
        print("2. Run: npm run dev")
        print("3. Open: http://localhost:3847")
        print()
    print("Keep this server running. Press Ctrl+C to quit.")


def _reset_tui() -> None:
    """Clear the TUI's alternate screen so the operator's shell isn't
    left in a half-painted state after a crash."""
    try:
        from .tui.display import TUIDisplay

        TUIDisplay().clear_screen()
    except Exception:  # pragma: no cover - defensive
        pass


__all__ = [
    "VALID_TRANSPORTS",
    "ServerConfig",
    "apply_runtime_flags",
    "bootstrap_server",
    "get_admin_token_from_db",
    "load_project_dotenv",
    "run_server",
]
