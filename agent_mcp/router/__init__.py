"""Always-on HTTP router that fronts per-project agent-mcp backends.

The router used to live in the nixos-developer-system deploy repo at
``users/dennis/agent-mcp/router.py`` + ``project_registry.py``. Phase
1a of the router-upstream plan (prancy-napping-pie) moved both files
in-tree so they can be tested, versioned, and shipped with the rest
of agent-mcp.

Public surface:

* ``agent_mcp.router.app``  — the aiohttp app + handlers + main()
* ``agent_mcp.router.project_registry`` — locking JSON registry

The primary entrypoint is the ``router`` subcommand on the
``agent-mcp`` CLI (``agent_mcp.cli:cli``). ``python -m
agent_mcp.router`` is kept as an alias so the module can be invoked
directly when someone wants a minimal entrypoint.

NOTE: this ``__init__`` deliberately stays import-side-effect-free.
Importing ``agent_mcp.router`` MUST NOT pull in ``.app``, because the
app module reads several ``AGENT_MCP_*`` env vars at top level (and
``.read_text()``s an installer template) — only the deploy-mode
process and the ``router`` subcommand should pay that cost. Tests
that only need the project registry can do ``from agent_mcp.router
import project_registry`` cheaply. The ``main`` helper is exposed
via a ``__getattr__`` shim so ``from agent_mcp.router import main``
still works lazily.
"""

from __future__ import annotations

from typing import Any

__all__ = ["main"]


def __getattr__(name: str) -> Any:  # pragma: no cover - thin lazy shim
    if name == "main":
        from .app import main as _main

        return _main
    raise AttributeError(f"module 'agent_mcp.router' has no attribute {name!r}")
