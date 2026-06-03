"""Entry point for ``python -m agent_mcp.router``.

Delegates to :func:`agent_mcp.router.app.main`. The recommended
invocation today is ``agent-mcp router …`` (or ``python -m
agent_mcp.cli router …``); this module hook is kept as a minimal
escape hatch for tooling that wants to skip the click CLI.

Import is deferred to ``main()`` because ``agent_mcp.router.app``
reads several ``AGENT_MCP_*`` env vars at module load and we don't
want that side effect from a bare ``python -c 'import …'``.
"""

from __future__ import annotations


def _run() -> None:
    from .app import main

    main()


if __name__ == "__main__":
    _run()
