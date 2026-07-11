# Agent-MCP/agent_mcp/__main__.py
"""``python -m agent_mcp`` entrypoint.

The ``.env`` discovery walk this module used to run inline (a 1-level
parent walk, narrower than — and redundant with — the 3-level walk
``cli.py`` runs on every invocation anyway, since this module always
imports ``.cli``) now lives solely in ``cli.py`` via
``core.env_boot.discover_and_load_dotenv`` (arch-r4 #11a). This module
is just the entrypoint shim.
"""

from .cli import main_cli

if __name__ == "__main__":
    main_cli()
