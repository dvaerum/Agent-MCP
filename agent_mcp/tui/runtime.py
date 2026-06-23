"""TUI display loop — lifted out of ``cli.py`` into its own module.

The display loop is a long-running anyio task that paints the server-
status / next-steps view to the operator's terminal while the MCP
backend is running. It used to be defined as a nested function inside
``server_cmd`` in ``cli.py``; pulling it out here lets both transport
runners in ``server_bootstrap`` share the same loop and keeps
``cli.py`` a thin click adapter.
"""

from __future__ import annotations

from typing import Any

import anyio

from ..core.config import logger
from .colors import TUITheme
from .display import TUIDisplay


async def tui_display_loop(
    cli_port: int,
    cli_transport: str,
    cli_project_dir: str,
    *,
    task_status: Any = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Repaint the operator-facing status view until shutdown.

    Started by ``server_bootstrap._run_sse`` / ``_run_stdio`` via
    ``task_group.start`` — the ``task_status.started()`` call below is
    what unblocks the parent's ``.start()``.

    The loop runs while ``globals.server_running`` is True; the
    bootstrap's teardown clears that flag, which exits the loop after
    the next sleep tick. Display refresh is intentionally slow (5s) —
    this view is for humans, not telemetry.
    """
    task_status.started()
    logger.info("TUI display loop started.")
    tui = TUIDisplay()
    initial_display = True

    from ..core import globals as globals_module

    async def get_server_status() -> dict[str, Any]:
        try:
            return {
                "running": globals_module.server_running,
                "status": "Running" if globals_module.server_running else "Stopped",
                "port": cli_port,
            }
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Error getting server status: %s", exc)
            return {
                "running": globals_module.server_running,
                "status": "Error",
                "port": cli_port,
            }

    try:
        # Wait briefly so the server has a chance to log its banner
        # before we paint over the screen.
        await anyio.sleep(2)

        tui.enable_alternate_screen()
        tui.hide_cursor()

        first_draw = True

        while globals_module.server_running:
            server_status = await get_server_status()

            if first_draw:
                tui.clear_screen()
                first_draw = False

            tui.move_cursor(1, 1)
            current_row = tui.draw_header(clear_first=False)

            tui.move_cursor(current_row, 1)
            tui.draw_status_bar(server_status)
            current_row += 2

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

            current_row += 2

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

            for row in range(current_row, tui.terminal_height):
                tui.move_cursor(row, 1)
                tui.clear_line()

            if initial_display:
                initial_display = False

            await anyio.sleep(5)
    except anyio.get_cancelled_exc_class():
        logger.info("TUI display loop cancelled.")
    finally:
        tui.show_cursor()
        tui.disable_alternate_screen()
        tui.clear_screen()
        print("MCP Server TUI has exited.")
        logger.info("TUI display loop finished.")


__all__ = ["tui_display_loop"]
