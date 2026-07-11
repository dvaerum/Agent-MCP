"""Guard: no test may resolve the PRODUCTION router-DB path.

``migrations_runner.get_router_db_path()`` falls back to the production
default ``/var/lib/agent-mcp/router.db`` when ``AGENT_MCP_ROUTER_DB`` is
unset — a system path shared across every pytest-xdist worker (and
unwritable on most machines). The root ``conftest._isolate_env`` floor
sets a per-test temp path so no test can ever fall through to it.

This guard pins that floor. If a future change drops it, this fails
loudly here — rather than silently re-introducing the intermittent
cross-file / cross-worker isolation hazard the floor exists to prevent.

Context: during the round-2 architecture-deepening loop, several agents
saw `test_sec_r8_type_confusion` / `test_sec_r9_sso_deprovision_bootstrap`
fail only under high-parallelism full-suite runs (never in isolation,
never on CI's matrix). The specific flake did not reproduce on clean main
(many full-suite passes, incl. under reinstall contention); this floor
closes the plausible root-cause footgun defensively.
"""

from __future__ import annotations

from pathlib import Path

from agent_mcp.router.migrations_runner import (
    _DEFAULT_ROUTER_DB,
    get_router_db_path,
)


def test_router_db_floor_prevents_production_default(tmp_path: Path) -> None:
    resolved = get_router_db_path()
    # The floor must have diverted us off the shared system default.
    assert resolved != _DEFAULT_ROUTER_DB, (
        "AGENT_MCP_ROUTER_DB floor missing — a test could resolve the "
        f"shared production path {_DEFAULT_ROUTER_DB}"
    )
    # This is a plain (non-router) test, so it gets the conftest floor,
    # which is keyed to this test's own tmp_path.
    assert resolved == (tmp_path / "router-floor.db").resolve()


def test_router_db_env_is_always_populated() -> None:
    import os

    assert os.environ.get("AGENT_MCP_ROUTER_DB"), (
        "the _isolate_env floor must set AGENT_MCP_ROUTER_DB for every test"
    )
