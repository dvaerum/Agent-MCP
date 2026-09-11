"""Shared test fixtures.

Phase F (prancy-napping-pie): `agent_mcp`'s app/tools/router/
repositories/features/external/tui/core/db/migrations cluster is
fully superseded by the Rust `conexus-*` workspace and deleted. Every
fixture/helper this module used to provide for in-process Starlette
integration testing (`reset_and_snapshot_globals`, `app`/`client`,
`seed_agent_row`, `existing_root_task_id`/`ensure_seed_root`,
`install_mock_ollama`/`mock_ollama`) had zero real consumers left once
the last test files exercising that code were deleted alongside it —
removed here rather than left as dead code with no test to prove it
still works. The remaining `tests/*.py` files are dashboard/Nix/
version/config-shape guards that don't need any of this.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# Module-level isolation: keep tests from accidentally hitting real APIs
# or reading the user's home OPENAI_API_KEY. No surviving test reads
# any of these, but this stays cheap insurance for whatever's added
# next under tests/ rather than something to prove is still load-
# bearing before removing it.
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DOTENV_PATH", "/dev/null")
    monkeypatch.delenv("MCP_PROJECT_DIR", raising=False)
