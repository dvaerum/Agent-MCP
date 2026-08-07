"""End-to-end reproducer for the production worker-auth-401 bug.

Production symptom (washing-brothers backend, 2026-06-04):
- DB has rows for backend-dev + ios-app-dev with status='created' and
  terminated_at=NULL.
- After systemctl restart, those workers' bearer tokens hit POST /mcp
  and return 401 ("invalid or missing agent bearer token") — even
  though the admin token works.

Root cause was identified in PR #115 (#99de576): a startup-ordering
race where `start_background_tasks` is awaited BEFORE `server.serve()`
(which is what triggers Starlette's lifespan → `application_startup`).
The session-registry pruner fires its first cycle against the wrong
SQLAlchemy engine cache because `MCP_PROJECT_DIR` isn't yet set, then
poisons the engine cache with the wrong DB URL.

PR #115's tests (`tests/test_lifespan_loads_active_agents.py`)
exercise `application_startup` in isolation. That's necessary but not
sufficient: it doesn't verify that, when the lifespan runs through
the FULL Starlette stack (uvicorn → asgi.lifespan → create_app's
`lifespan` context manager → `application_startup`), a POST /mcp with
a pre-existing worker bearer returns 200.

This test pins that end-to-end contract. The strategy:

  1. Pre-seed a worker `agents` row in the project DB BEFORE the
     lifespan opens (so the row is only in `g.active_agents` if
     lifespan actively loaded it).
  2. Drive the full lifespan via `tests/harness.py::mcp_session`
     (which uses Starlette's TestClient, which itself runs the
     lifespan exactly as uvicorn does).
  3. POST /mcp with the seeded worker bearer + JSON-RPC `initialize`.
  4. Assert 200.

A regression that re-introduces the race (or otherwise drops the
worker from `g.active_agents` post-lifespan) flips this from 200 to
401 — same wire shape Dennis sees in production.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_worker_agent_row_pre_lifespan(
    project_dir: Path, *, token: str, agent_id: str
) -> None:
    """INSERT a status='created' worker row into the project DB
    BEFORE the lifespan opens.

    `application_startup` will run `init_database` which uses
    `CREATE TABLE IF NOT EXISTS` — so creating the agents table here
    is idempotent with the lifespan's own schema init. The crucial
    contract: when lifespan's "Load Active Agents" step runs, our
    row must already be on disk and be picked up into
    `g.active_agents`.
    """
    db_path = project_dir / ".agent" / "mcp_state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                token TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                current_task TEXT,
                working_directory TEXT NOT NULL,
                color TEXT,
                terminated_at TEXT,
                updated_at TEXT,
                aoe_session_id TEXT
            )
            """
        )
        now = _dt.datetime.now().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO agents (token, agent_id, "
            "created_at, status, current_task, working_directory, color, "
            "terminated_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                agent_id,
                now,
                "created",
                None,
                "/tmp",
                "#abc123",
                None,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _post_mcp_initialize(client, bearer: str):
    """POST /mcp with `Authorization: Bearer <bearer>` and a minimal
    JSON-RPC `initialize` body. Matches what the real Streamable HTTP
    client (Claude Code, the dashboard) sends on connect."""
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-probe", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )


async def test_pre_seeded_worker_bearer_authenticates_through_full_lifespan(
    tmp_path: Path,
) -> None:
    """Pre-seed a worker `agents` row, run the full Starlette lifespan
    via `mcp_session` (TestClient → lifespan), then POST /mcp with the
    worker's bearer. The HTTP response MUST be 200, NOT 401.

    This is the regression test for the live washing-brothers bug:
    backend-dev + ios-app-dev existed in the DB as `status='created'`
    but the worker bearers 401'd after restart. If `application_startup`
    fails to populate `g.active_agents` for any reason (engine cache
    race, lifespan crash before line 345, signal-handler bug, etc.),
    the worker bearer at the HTTP layer hits `AuthHeaderMiddleware`'s
    `verify_token` check, which only reads `g.active_agents`, and
    returns 401.

    The harness uses `mcp_session` which already pre-creates the
    project_dir before calling `create_app`, so we drop our seed file
    into the project_dir's `.agent/` subdir BEFORE entering
    `mcp_session`'s context. Lifespan-during-context-enter then loads
    our row.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)

    worker_token = "live_repro_worker_bearer_0123456789abcdef"
    worker_agent_id = "live-repro-backend-dev"
    _seed_worker_agent_row_pre_lifespan(
        project_dir, token=worker_token, agent_id=worker_agent_id
    )

    async with mcp_session(tmp_path) as admin:
        # Sanity: admin bearer still works.
        r_admin = _post_mcp_initialize(admin.client, admin.admin_token)
        assert r_admin.status_code == 200, (
            f"admin bearer rejected post-lifespan: {r_admin.status_code} "
            f"{r_admin.text!r}"
        )

        # The regression check: worker bearer pre-seeded into the DB
        # MUST authenticate after lifespan finishes.
        r_worker = _post_mcp_initialize(admin.client, worker_token)
        assert r_worker.status_code == 200, (
            f"Pre-seeded worker bearer was rejected with "
            f"{r_worker.status_code} after full lifespan ran. "
            f"Body: {r_worker.text!r}. This is the live "
            f"washing-brothers 401 regression — lifespan didn't load "
            f"the worker into g.active_agents (or AuthHeaderMiddleware "
            f"is reading a stale snapshot)."
        )


async def test_pre_seeded_worker_lands_in_g_active_agents_after_lifespan(
    tmp_path: Path,
) -> None:
    """Sibling check at the in-memory layer.

    The HTTP test above asserts the symptom (401 vs 200); this asserts
    the cause (whether the row made it into `g.active_agents`). Both
    failing in tandem points at the lifespan load step; only the HTTP
    test failing points at the middleware or context-binding path.
    """
    from agent_mcp.core import globals as g

    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)

    worker_token = "live_repro_inmem_worker_bearer_fedcba9876543210"
    worker_agent_id = "live-repro-ios-app-dev"
    _seed_worker_agent_row_pre_lifespan(
        project_dir, token=worker_token, agent_id=worker_agent_id
    )

    async with mcp_session(tmp_path):
        assert worker_token in g.active_agents, (
            f"Worker bearer missing from g.active_agents after full "
            f"Starlette lifespan. Present keys: "
            f"{list(g.active_agents.keys())!r}. "
            f"This is the underlying in-memory regression that produces "
            f"the 401 at the HTTP layer."
        )
        assert g.active_agents[worker_token]["agent_id"] == worker_agent_id
