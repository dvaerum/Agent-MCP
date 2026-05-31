"""Shared test fixtures for in-process integration testing.

The goal is to make integration tests cheap to write: spin up agent-mcp
as an in-process Starlette app, hit it with httpx via Starlette's
TestClient (which handles lifespan startup/shutdown), assert behavior.

No systemd, no real Ollama, no network. Each test gets a fresh tmpdir
SQLite DB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest


# Module-level isolation: keep tests from accidentally hitting real APIs
# or reading the user's home OPENAI_API_KEY.
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty key → openai_service.initialize_openai_client() short-circuits
    # to None (graceful degrade); no models.list() call goes out.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    # Don't load whatever .env happens to be in the cwd.
    monkeypatch.setenv("DOTENV_PATH", "/dev/null")
    # Belt and suspenders against stray dashboard ports getting probed.
    monkeypatch.delenv("MCP_PROJECT_DIR", raising=False)


@pytest.fixture
def reset_globals() -> Iterator[None]:
    """Reset agent_mcp.core.globals state between tests.

    agent-mcp uses a module-level singleton (g.admin_token,
    g.openai_client_instance, in-memory task/agent caches, etc.). Tests
    that build their own app instances must reset these or state leaks
    across tests in surprising ways.

    Also resets `agent_mcp.db.write_queue._global_write_queue` — that
    singleton gets stopped during lifespan shutdown, and the next test
    inheriting the dead instance hangs forever waiting for a worker
    that no longer exists.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db import write_queue as _wq

    # Force a fresh write queue for this test by clearing the singleton
    # cache before lifespan startup.
    _wq._global_write_queue = None

    snapshot = {
        "connections": dict(g.connections),
        "active_agents": dict(g.active_agents),
        "admin_token": g.admin_token,
        "tasks": dict(g.tasks),
        "file_map": dict(g.file_map),
        "agent_working_dirs": dict(g.agent_working_dirs),
        "agent_tmux_sessions": dict(g.agent_tmux_sessions),
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
    g.admin_token = snapshot["admin_token"]
    g.tasks.clear()
    g.tasks.update(snapshot["tasks"])
    g.file_map.clear()
    g.file_map.update(snapshot["file_map"])
    g.agent_working_dirs.clear()
    g.agent_working_dirs.update(snapshot["agent_working_dirs"])
    g.agent_tmux_sessions.clear()
    g.agent_tmux_sessions.update(snapshot["agent_tmux_sessions"])
    g.audit_log.clear()
    g.audit_log.extend(snapshot["audit_log"])
    g.openai_client_instance = snapshot["openai_client_instance"]
    g.global_vss_load_tested = snapshot["global_vss_load_tested"]
    g.global_vss_load_successful = snapshot["global_vss_load_successful"]
    _wq._global_write_queue = None


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
    """
    from starlette.testclient import TestClient

    with TestClient(app) as test_client:
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
