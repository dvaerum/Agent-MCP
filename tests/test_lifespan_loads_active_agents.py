"""Lifespan startup must populate `g.active_agents` from the DB.

Live-bug repro: the `washing-brothers` backend was restarted after a
worker agent's row was flipped back from `terminated` to `created`.
The worker's bearer token still authenticated as 401, even though the
admin token worked. Investigation showed `g.active_agents` was empty
on the live process — the lifespan-startup "load agents" step never
deposited the worker into the in-memory map.

Root cause was a startup-ordering race in `cli.py::run_sse_server_with_bg_tasks`:
`start_background_tasks(tg)` was awaited *before* `server.serve()`,
which is what triggers Starlette's lifespan → `application_startup`.
The session-registry pruner immediately fired (asyncio.to_thread to
`session_registry.expire_stale`), and that path resolves the project
DB via the SQLAlchemy `get_engine()` → `get_db_path()` chain. With
`MCP_PROJECT_DIR` not yet set (the env var is only set inside
`application_startup`, line 157), `get_project_dir()` falls back to
`Path(".")` and the engine cache binds to a wrong DB URL. Later code
paths that share the engine cache then see "no such table:
mcp_sessions" (and worse, queries against the wrong file return
empty results).

The fix is to gate background-task startup on lifespan completion:
expose `g.startup_complete_event` (an `asyncio.Event`) set at the
end of `application_startup`, and have background tasks `.wait()` on
it before their first cycle.

These tests verify two things:

  1. `application_startup` populates `g.active_agents` with rows
     whose `status != 'terminated'` (the contract that lifespan keeps
     the in-memory map in sync with the DB on each boot).

  2. The startup event signal is set *after* `application_startup`
     finishes — so background tasks that wait on it cannot race with
     DB initialization.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path

import pytest

from agent_mcp.core import globals as g


pytestmark = pytest.mark.asyncio


async def _run_application_startup(project_dir: Path) -> None:
    """Run `application_startup` in isolation against `project_dir`.

    Mirrors what Starlette's lifespan does, minus the rest of the
    `create_app` wiring — keeps the test focused on the state-load step
    rather than HTTP plumbing.
    """
    from agent_mcp.app.server_lifecycle import application_startup

    await application_startup(
        project_dir_path_str=str(project_dir), admin_token_param=None
    )


def _seed_worker_agent_row(
    project_dir: Path, *, token: str, agent_id: str, status: str
) -> None:
    """INSERT a worker `agents` row directly into the project DB.

    The lifespan-startup state-load reads `SELECT ... FROM agents
    WHERE status != 'terminated'`. To exercise the bug we need a row
    that *should* be loaded (status='created') but the in-memory map
    fails to pick up — so we have to seed before lifespan runs.
    """
    import sqlite3

    db_path = project_dir / ".agent" / "mcp_state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # The schema is created by `application_startup` → `init_database`.
    # Run a minimal init here so we can pre-seed before lifespan starts.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                token TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL UNIQUE,
                capabilities TEXT,
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
            "INSERT OR REPLACE INTO agents (token, agent_id, capabilities, "
            "created_at, status, current_task, working_directory, color, "
            "terminated_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                agent_id,
                "[]",
                now,
                status,
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


async def test_lifespan_loads_non_terminated_agent_into_active_map(
    tmp_path: Path, reset_globals: None
) -> None:
    """Seed a `status='created'` worker row, then run lifespan: the
    worker's bearer token must end up in `g.active_agents` with the
    seeded `agent_id`.

    This is the live-bug contract: post-restart, a worker that was
    `created` in DB MUST authenticate via its bearer. The auth path
    (`agent_mcp/core/auth.py::verify_token`) only looks at
    `g.active_agents`; if startup doesn't populate that, the worker
    is locked out until manual intervention.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    worker_token = "lifespan_test_token_abc123"
    worker_agent_id = "backend-dev-test"
    _seed_worker_agent_row(
        project_dir,
        token=worker_token,
        agent_id=worker_agent_id,
        status="created",
    )

    await _run_application_startup(project_dir)

    assert worker_token in g.active_agents, (
        f"Worker bearer token missing from g.active_agents after lifespan. "
        f"Keys present: {list(g.active_agents.keys())}. "
        f"This means a restored agent stays locked out (401) post-restart "
        f"until admin manually re-adds it."
    )
    assert g.active_agents[worker_token]["agent_id"] == worker_agent_id


async def test_lifespan_skips_terminated_agents(
    tmp_path: Path, reset_globals: None
) -> None:
    """Terminated rows MUST NOT land in `g.active_agents`.

    The auth path uses `g.active_agents` as the allow-list for
    bearer tokens. A terminated worker's bearer must NOT auth — that's
    the soft-delete contract. This guards against an over-eager "load
    everything" fix to the previous test.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    terminated_token = "lifespan_test_terminated_token_xyz789"
    _seed_worker_agent_row(
        project_dir,
        token=terminated_token,
        agent_id="terminated-worker-test",
        status="terminated",
    )

    await _run_application_startup(project_dir)

    assert terminated_token not in g.active_agents, (
        f"Terminated worker's bearer leaked into g.active_agents: "
        f"{g.active_agents.get(terminated_token)!r}. Soft-delete broken."
    )


async def test_lifespan_signals_startup_complete_event(
    tmp_path: Path, reset_globals: None
) -> None:
    """After `application_startup` returns, `g.startup_complete_event`
    must be set.

    Background tasks (session-registry pruner, message-retention pruner,
    RAG indexer) that touch the DB via the SQLAlchemy engine cache
    cannot run until `application_startup` has set `MCP_PROJECT_DIR`
    and applied migrations. Exposing a sentinel `asyncio.Event` lets
    those tasks `await g.startup_complete_event.wait()` on entry,
    deferring their first cycle until the cache is bound to the right
    DB file.

    This test asserts the contract: lifespan SETS the event by the
    time it returns.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # Sanity: event should exist and be unset before lifespan runs.
    assert hasattr(g, "startup_complete_event"), (
        "g.startup_complete_event missing — background tasks have no "
        "way to defer their first cycle until lifespan finishes."
    )
    assert isinstance(g.startup_complete_event, asyncio.Event)
    assert not g.startup_complete_event.is_set(), (
        "Event already set at test entry — reset_globals fixture should "
        "have cleared it. Cross-test state leak likely."
    )

    await _run_application_startup(project_dir)

    assert g.startup_complete_event.is_set(), (
        "g.startup_complete_event was NOT set by application_startup. "
        "Background tasks that wait on it will block forever."
    )
