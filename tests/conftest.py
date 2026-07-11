"""Shared test fixtures for in-process integration testing.

The goal is to make integration tests cheap to write: spin up agent-mcp
as an in-process Starlette app, hit it with httpx via Starlette's
TestClient (which handles lifespan startup/shutdown), assert behavior.

No systemd, no real Ollama, no network. Each test gets a fresh tmpdir
SQLite DB.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest


# Module-level isolation: keep tests from accidentally hitting real APIs
# or reading the user's home OPENAI_API_KEY.
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Empty key → openai_service.initialize_openai_client() short-circuits
    # to None (graceful degrade); no models.list() call goes out.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    # Don't load whatever .env happens to be in the cwd.
    monkeypatch.setenv("DOTENV_PATH", "/dev/null")
    # Belt and suspenders against stray dashboard ports getting probed.
    monkeypatch.delenv("MCP_PROJECT_DIR", raising=False)
    # create_agent spins up a real tmux session and sleeps 1s between
    # ~6 setup commands (6s/test) in production. Tests don't need the
    # settle delay — zero it so create_agent tests aren't dominated by
    # blocking sleeps.
    monkeypatch.setenv("AGENT_MCP_AGENT_SETUP_DELAY", "0")
    # Router-DB isolation FLOOR. ``migrations_runner.get_router_db_path()``
    # falls back to the PRODUCTION default ``/var/lib/agent-mcp/router.db``
    # when ``AGENT_MCP_ROUTER_DB`` is unset — a system path that is shared
    # across every pytest-xdist worker (and unwritable on most machines).
    # Any test that touches router code without setting its own DB would
    # either crash on that path or, worse, share one file across workers.
    # Guarantee a per-test temp floor so no test can ever fall through to
    # the system default. Router tests that need a specific DB still
    # override this via their own ``monkeypatch.setenv`` (last write wins;
    # this autouse fixture runs first). Set only when absent so an
    # already-configured (e.g. session-level) override is respected.
    if not os.environ.get("AGENT_MCP_ROUTER_DB"):
        monkeypatch.setenv(
            "AGENT_MCP_ROUTER_DB", str(tmp_path / "router-floor.db")
        )


@pytest.fixture
def reset_globals() -> Iterator[None]:
    """Reset agent_mcp.core.globals state between tests.

    agent-mcp uses a module-level singleton (g.openai_client_instance,
    in-memory task/agent caches, etc.). Tests that build their own app
    instances must reset these or state leaks across tests in
    surprising ways.

    """
    from agent_mcp.core import globals as g
    from agent_mcp.db import engine as _engine

    # Drop SQLAlchemy engines bound to a previous test's tmp DB
    # path — each test gets its own project_dir + DB.
    _engine.reset_engine_cache()
    # wait_for_events Phase 2: drop signals bound to a prior test's
    # event loop. asyncio.Event instances cannot be awaited across
    # loops; signal_for() lazily recreates as needed.
    g.agent_event_signals.clear()
    # PR-2 event-coord: locks and queues are also per-test (locks bound
    # to event loop; queues are transient by design). PR-B / v5.0.24
    # added the per-waiter queue registry; clear it for the same
    # reason — asyncio.Queue is bound to its loop.
    g.agent_event_locks.clear()
    g.agent_event_queues.clear()
    g.agent_event_waiters.clear()
    # Lifespan startup-complete sentinel: every test starts with a
    # fresh, cleared Event so we can detect a regression where
    # application_startup forgets to set it.
    g.reset_startup_complete_event()

    snapshot = {
        "connections": dict(g.connections),
        "active_agents": dict(g.active_agents),
        # retire-system-token Wave 3: ``g.admin_token`` is deleted as a
        # declared global. The conftest ``client`` fixture still sets it
        # dynamically as an attribute (see fixture docstring); capture
        # defensively so the snapshot survives when no test has set it.
        "admin_token": getattr(g, "admin_token", None),
        "tasks": dict(g.tasks),
        "file_map": dict(g.file_map),
        "agent_working_dirs": dict(g.agent_working_dirs),
        "audit_log": list(g.audit_log),
        "openai_client_instance": g.openai_client_instance,
        "global_vss_load_tested": g.global_vss_load_tested,
        "global_vss_load_successful": g.global_vss_load_successful,
    }
    yield
    # Restore. Dicts/lists are mutated in place, so clear then update.
    g.connections.clear()
    g.connections.update(snapshot["connections"])
    g.active_agents.clear()
    g.active_agents.update(snapshot["active_agents"])
    # retire-system-token Wave 3: only restore admin_token if a prior
    # test (via the ``client`` fixture) dynamically set it; otherwise
    # leave the attr absent so reads via ``getattr(g, "admin_token", ...)``
    # behave consistently across tests.
    if snapshot["admin_token"] is not None:
        g.admin_token = snapshot["admin_token"]
    elif hasattr(g, "admin_token"):
        delattr(g, "admin_token")
    g.tasks.clear()
    g.tasks.update(snapshot["tasks"])
    g.file_map.clear()
    g.file_map.update(snapshot["file_map"])
    g.agent_working_dirs.clear()
    g.agent_working_dirs.update(snapshot["agent_working_dirs"])
    g.audit_log.clear()
    g.audit_log.extend(snapshot["audit_log"])
    g.openai_client_instance = snapshot["openai_client_instance"]
    g.global_vss_load_tested = snapshot["global_vss_load_tested"]
    g.global_vss_load_successful = snapshot["global_vss_load_successful"]
    _engine.reset_engine_cache()
    g.agent_event_signals.clear()
    g.agent_event_locks.clear()
    g.agent_event_queues.clear()
    g.agent_event_waiters.clear()
    g.reset_startup_complete_event()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A fresh workspace directory for one test.

    agent-mcp will create `<project_dir>/.agent/mcp_state.db` inside it
    during application startup.
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    return workspace


@pytest.fixture
def app(project_dir: Path, reset_globals: None):
    """A Starlette app instance pointed at a fresh project dir.

    Use with TestClient (`client` fixture) for HTTP testing. The
    TestClient context manager runs the lifespan, which initializes the
    DB schema, generates the admin token, and runs the rest of
    `application_startup`.
    """
    from agent_mcp.app.main_app import create_app

    return create_app(project_dir=str(project_dir))


@pytest.fixture
def client(app):
    """An httpx TestClient against the in-process app.

    Using it as a context manager triggers lifespan startup/shutdown.
    Routes are reachable as `client.get("/api/tokens")` etc.

    retire-system-token Wave 1: pre-Wave-1, tests using this fixture
    passed ``g.admin_token`` (the system bearer) as ``body['token']``
    on REST mutation routes, and the dep admitted via the god-key
    check. That check is gone; we seed a real per-agent manager-role
    row (post-lifespan) so the body-token path through
    ``_bearer_is_operator_tier`` admits via
    ``verify_token(token, "manager")`` — same wire shape, real
    per-principal credential.

    retire-system-token Wave 3: ``g.admin_token`` is deleted as a
    declared global in ``agent_mcp.core.state``. This fixture still
    assigns ``g.admin_token = token`` dynamically (Python attribute
    assignment works on the state module without prior declaration) so
    the many tests that read ``g.admin_token`` as their operator-tier
    bearer continue to function without per-callsite edits. The
    snapshot/restore plumbing in ``reset_globals`` reads it
    defensively via ``getattr``.
    """
    import datetime as _dt
    import secrets as _secrets

    from starlette.testclient import TestClient

    with TestClient(app) as test_client:
        # Seed a manager-role agent row that the dep's
        # ``_bearer_is_operator_tier`` admits, and re-point
        # ``g.admin_token`` at that row's token so existing tests
        # (which dereference ``g.admin_token`` as their operator-tier
        # bearer / body-token credential) keep working without per-
        # callsite edits.
        from agent_mcp.core import globals as g
        from agent_mcp.db.connection import get_db_connection

        token = _secrets.token_hex(16)
        now = _dt.datetime.now().isoformat()
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO agents (token, agent_id, "
                "capabilities, created_at, status, working_directory, "
                "color, updated_at, agent_role) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token,
                    "admin",
                    "[]",
                    now,
                    "active",
                    "/tmp",
                    "#888",
                    now,
                    "manager",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        g.active_agents[token] = {
            "agent_id": "admin",
            "status": "active",
            "created_at": now,
            "capabilities": [],
            "agent_role": "manager",
        }
        g.admin_token = token
        yield test_client


@pytest.fixture
def mock_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the OpenAI-shaped embeddings endpoint with an in-process fake.

    Tests that exercise RAG/indexing flows opt in by depending on this
    fixture. Returns deterministic 1024-dim zero-vectors (matching the
    `qwen3-embedding:0.6b` dimension used by the deployment).

    Not strictly needed for Phase 1's smoke test, but provided so Phase
    3+ tests don't each invent their own mock.
    """
    import httpx

    DIM = 1024

    def _handler(request: httpx.Request) -> httpx.Response:
        # /v1/embeddings is the OpenAI shape Ollama serves
        if request.url.path.endswith("/embeddings"):
            body = request.read()
            # naive: count inputs by occurrences of "input" key in body
            # OpenAI spec: {"input": str | list[str], "model": "..."}
            import json as _json

            data = _json.loads(body) if body else {}
            inputs = data.get("input", "")
            if isinstance(inputs, str):
                inputs = [inputs]
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "embedding": [0.0] * DIM, "index": i}
                        for i in range(len(inputs))
                    ],
                    "model": data.get("model", "mock-embed"),
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(_handler)
    # Patch httpx.Client and httpx.AsyncClient defaults; agent-mcp's
    # openai client uses httpx under the hood.
    original_client_init = httpx.Client.__init__
    original_async_init = httpx.AsyncClient.__init__

    def _patched_client_init(self, *args, **kwargs):
        kwargs.setdefault("transport", transport)
        original_client_init(self, *args, **kwargs)

    def _patched_async_init(self, *args, **kwargs):
        kwargs.setdefault("transport", transport)
        original_async_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _patched_client_init)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_async_init)
