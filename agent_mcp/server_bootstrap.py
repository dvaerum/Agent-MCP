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
  2. ``apply_runtime_flags`` — translates the ``advanced`` /
     ``no_index`` / ``debug`` flags into the side effects the rest of
     the codebase reads (``core.config`` knobs, ``MCP_DEBUG`` env var).
  3. ``bootstrap_server`` — builds the Starlette app (sse) or returns
     ``(None, teardown)`` (stdio).
  4. ``run_server`` — async runners (sse uvicorn + bg tasks, stdio
     mcp pump). Called by the CLI subcommand callback after
     ``ServerConfig.from_cli_args``.

The teardown returned by ``bootstrap_server`` is idempotent — the CLI
runner may invoke it on both the normal-exit and SystemExit paths and
the test contract pins that calling it twice does not raise.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

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
        forwarding_hmac_in_path: Optional[str] = None,
    ) -> "ServerConfig":
        """Build a config from the keyword set the click ``server``
        subcommand passes to its callback.

        Normalises ``project_dir`` to an absolute path so background
        tasks that fire after a ``chdir`` (the deploy repo's pre-start
        hook does this) still resolve the right ``.agent`` directory.
        Validation happens via ``__post_init__`` so callers always
        catch bad input synchronously.
        """
        resolved_project = str(Path(project_dir).resolve())
        return cls(
            transport=transport.lower(),
            port=int(port),
            project_dir=resolved_project,
            uds=uds,
            forwarding_hmac_in_path=forwarding_hmac_in_path,
            debug=bool(debug),
            no_tui=bool(no_tui),
            advanced=bool(advanced),
            git=bool(git),
            no_index=bool(no_index),
        )


# --- runtime-flag → side-effect translation --------------------------


def apply_runtime_flags(config: ServerConfig) -> None:
    """Translate the boolean flags on ``config`` into the global side
    effects the rest of the codebase reads.

    Specifically:

    * ``--advanced`` flips ``core.config.ADVANCED_EMBEDDINGS`` — the
      ONE global this function mutates for embeddings. Readers resolve
      ``(model, dimension)`` by calling ``core.config.embedding_settings()``
      at point of use rather than importing a value that would freeze
      at their own import time; see that function's docstring.
    * ``--no-index`` flips ``core.config.DISABLE_AUTO_INDEXING``.
    * ``--debug`` exports ``MCP_DEBUG=true|false`` — Starlette reads
      this at app construction time inside ``create_app``.

    Idempotent: calling it twice with the same config is a no-op
    (the writes are deterministic).
    """
    from .core import config as core_config

    # Embeddings. ``ADVANCED_EMBEDDINGS`` is the single global this sets;
    # every reader resolves (model, dimension) via
    # ``core_config.embedding_settings()`` at the point of use, so
    # there's nothing else to keep in sync here.
    core_config.ADVANCED_EMBEDDINGS = bool(config.advanced)
    settings = core_config.embedding_settings(config.advanced)
    if config.advanced:
        logger.info(
            "Advanced embeddings mode enabled (%d dimensions, %s).",
            settings.dimension,
            settings.model,
        )
    else:
        # Default is the simple model; only log so operators see which
        # mode is active.
        logger.info(
            "Using simple embeddings mode (%d dimensions, %s).",
            settings.dimension,
            settings.model,
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
    # F015 v7: do NOT ``.strip()`` raw HMAC bytes. The file is binary
    # (32 bytes of /dev/urandom written by the systemd unit's
    # ExecStartPre) and any of those bytes can legitimately be ASCII
    # whitespace (``\n``, ``\r``, `` ``, ``\t`` etc.). The router
    # (``router/project_orchestrator.py::ensure_forwarding_hmac_key``)
    # does NOT strip — it signs with the full bytes. Backend stripping
    # silently shortens the key, every HMAC verify fails, every
    # cookie-authenticated request 401s. The first observed live VM
    # key was 0x0a (\n) leading, exhibiting exactly this. Reject
    # empty; accept everything else as-is.
    if not data:
        logger.warning(
            "Forwarding-hmac key file %s is empty; forwarding-header "
            "auth stays dormant.",
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
      2. (sse only) ``create_app(project_dir)``
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
        # SC-2 / SD-3: suppress uvicorn's ``Server: uvicorn`` banner. On a
        # direct bind (no nginx masking) the version-free framework name is
        # still a needless fingerprint; defence-in-depth against CVE-matching.
        server_header=False,
    )
    if config.uds:
        uvicorn_kwargs["uds"] = config.uds
    else:
        # Bind loopback by default. The router↔backend proxy always
        # talks to the backend over its Unix socket (the ``config.uds``
        # branch above; see ``router/app.py::_ensure`` +
        # ``_proxy_to_backend``), so the TCP listener is only for
        # direct/standalone use and has no reason to be
        # internet-reachable. Operators who genuinely need an external
        # TCP bind opt in via ``AGENT_MCP_MCP_HOST`` (mirrors the
        # router's ``AGENT_MCP_ROUTER_HOST`` env-override pattern).
        host = os.environ.get("AGENT_MCP_MCP_HOST", "127.0.0.1")
        uvicorn_kwargs["host"] = host
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
                f"uds {config.uds}"
                if config.uds
                else f"http://{uvicorn_kwargs['host']}:{config.port}",
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
    "run_server",
]
