"""Legacy /api alias routes removed in v5.0.0.

Two of the Starlette dashboard routes were duplicates — kept around
during the URL redesign for backwards compatibility:

  /api/agents-list  → same handler as /api/agents
  /api/tasks-all    → same handler as /api/tasks

Both are dropped here. Callers must use the canonical paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_legacy_agents_list_returns_404(tmp_path: Path) -> None:
    """The legacy alias no longer dispatches the GET to the agents list
    handler. Starlette returns 405 (URL matched by the generic OPTIONS
    catch-all at /api/{path:path} but no GET method registered) rather
    than 404 — both confirm the alias is gone."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/agents-list")
        assert r.status_code in (404, 405)


async def test_legacy_tasks_all_returns_404(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/tasks-all")
        assert r.status_code in (404, 405)


async def test_canonical_agents_still_works(tmp_path: Path) -> None:
    """Regression guard: deleting the legacy alias didn't touch /api/agents."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/agents")
        assert r.status_code == 200


async def test_canonical_tasks_still_works(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/tasks")
        assert r.status_code == 200
