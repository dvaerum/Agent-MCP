"""GET /mcp lifecycle wires session_registry → SSE notifications.

Background
----------

PR #69 shipped `agent_mcp/core/session_registry.py` (`register_session`,
`attach_runtime_queue`, `fanout_to_agent`, `detach_runtime_queue`,
`unregister_session`). PR #63 wired `notify_agent_inbox()` to call
`fanout_to_agent(agent_id, payload)` with a
`notifications/resources/updated` envelope.

But before this PR, NO call site existed for `register_session` or
`attach_runtime_queue`. Writers happily fanned out to an empty
`_runtime_queues` map; every notification was silently dropped at the
wire. The in-process `signal_for()` long-poll path was the only working
delivery channel.

This module pins the transport-layer wiring: opening a GET /mcp stream
must register a session row + attach an asyncio queue. Closing must
detach + unregister. A pump task drains the queue onto the SSE stream
as `data: <json-rpc envelope>\n\n` frames.

Why uvicorn instead of httpx ASGITransport
------------------------------------------

Long-lived SSE streams + Starlette's TestClient or httpx
ASGITransport are an impedance mismatch: TestClient blocks the
event loop on streaming reads; ASGITransport's `client.stream`
sometimes won't return the response until the server flushes its
first body chunk. The simplest robust answer is uvicorn on an
ephemeral port + a real httpx client — same shape production runs.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio


pytestmark = pytest.mark.asyncio


def _seed_worker(name: str) -> str:
    """Insert an agent row + active_agents entry. Return its bearer token."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    worker_token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (worker_token, name, "[]", now, "active", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()

    g.active_agents[worker_token] = {
        "agent_id": name,
        "status": "active",
        "created_at": now,
        "capabilities": [],
    }
    return worker_token


def _mcp_sessions_count_for(agent_id: str) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM mcp_sessions WHERE agent_id = ?",
            (agent_id,),
        )
        return cur.fetchone()["n"]
    finally:
        conn.close()


def _runtime_queues_size() -> int:
    from agent_mcp.core import session_registry as reg

    return len(reg._runtime_queues)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UvicornInThread:
    """Launch uvicorn on an ephemeral port from a worker thread.

    Each instance gets its own thread + uvicorn.Server so the suite
    can run tests in parallel under pytest-xdist (each worker is a
    separate process anyway, but inside a single worker the in-thread
    server's lifespan owns this process's module-level globals).
    """

    def __init__(self, app) -> None:
        import uvicorn

        self.port = _free_port()
        self.config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning",
            lifespan="on", loop="asyncio",
        )
        self.server = uvicorn.Server(self.config)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        # Wait for the port to accept connections.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("uvicorn did not come up in time")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=15)


@pytest_asyncio.fixture
async def live_server(tmp_path: Path) -> AsyncIterator[tuple[str, str]]:
    """Boot the Starlette app under uvicorn on a fresh ephemeral port.

    Yields `(base_url, admin_token)`. Tears the server down + restores
    process-wide globals on exit (mirrors `conftest._reset_globals` so
    a later test using the in-process `client` fixture sees a clean
    slate).
    """
    import os
    from agent_mcp.core import globals as g
    from agent_mcp.db import engine as _engine

    env_snapshot = {
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
        "DOTENV_PATH": os.environ.get("DOTENV_PATH"),
        "MCP_PROJECT_DIR": os.environ.get("MCP_PROJECT_DIR"),
    }
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["DOTENV_PATH"] = "/dev/null"
    os.environ.pop("MCP_PROJECT_DIR", None)

    _engine.reset_engine_cache()
    g.agent_event_signals.clear()

    snapshot = {
        "connections": dict(g.connections),
        "active_agents": dict(g.active_agents),
        # retire-system-token Wave 3: ``g.admin_token`` is deleted as
        # a declared global. Capture defensively in case an earlier
        # test set it as a dynamic attribute.
        "admin_token": getattr(g, "admin_token", None),
        "tasks": dict(g.tasks),
        "file_map": dict(g.file_map),
        "agent_working_dirs": dict(g.agent_working_dirs),
        "audit_log": list(g.audit_log),
        "global_vss_load_tested": g.global_vss_load_tested,
        "global_vss_load_successful": g.global_vss_load_successful,
    }

    # We DON'T install the global httpx-init mock-ollama patch here:
    # the test fixture itself uses live httpx clients against uvicorn
    # at 127.0.0.1:<port>, and a global mock-transport patch would
    # short-circuit those to a 404-returning mock. Lifespan startup no
    # longer makes any eager OpenAI/Ollama network call (arch-r4 #2
    # removed the boot-time client-init round-trip), so there's nothing
    # to keep from going out here. Tests that need embeddings calls
    # answered should add their own narrower mock around just those
    # code paths.
    original_client_init = httpx.Client.__init__
    original_async_init = httpx.AsyncClient.__init__

    from agent_mcp.app.main_app import create_app

    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    app = create_app(project_dir=str(project_dir))

    server = _UvicornInThread(app)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"
    try:
        # Wait for lifespan startup to complete by polling a route. The
        # uvicorn worker thread can serve socket connections before
        # `lifespan.startup` finishes; routes installed inside
        # `application_startup` (admin token, schema migration) need that
        # to complete before any /api/* route works.
        #
        # Wave 1 of prancy-napping-pie put `/api/tokens` behind
        # `require_operator_session`. We poll without auth and treat
        # 401 as the "ready, but you need a bearer" signal (handler
        # reached → lifespan finished). retire-system-token Wave 3
        # deleted ``g.admin_token`` so we use the 401 status alone as
        # the readiness signal — the per-agent admin bearer is minted
        # below.
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as bootstrap:
            deadline = time.monotonic() + 20
            last_status = None
            ready = False
            while time.monotonic() < deadline:
                try:
                    r = await bootstrap.get("/api/tokens")
                    last_status = r.status_code
                    # 200 = legacy (unauthenticated allowed); 401 =
                    # post-Wave-1 (dep reached, lifespan done). Either
                    # indicates the handler is wired and lifespan is
                    # complete.
                    if r.status_code in (200, 401):
                        ready = True
                        break
                except httpx.HTTPError:  # pragma: no cover - boot races
                    pass
                await asyncio.sleep(0.1)
            if not ready:
                raise RuntimeError(
                    f"/api/tokens never reachable (last status: {last_status})"
                )

        # retire-system-token Wave 1: the system_token bearer no
        # longer authenticates ``/mcp``; mint a real per-agent
        # manager-role token (the new operator-tier bearer surface)
        # so the test's ``Authorization: Bearer <admin_token>``
        # calls keep working.
        import datetime as _dt
        import secrets as _secrets

        from agent_mcp.db.connection import get_db_connection

        admin_token = _secrets.token_hex(16)
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
                    admin_token,
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
        g.active_agents[admin_token] = {
            "agent_id": "admin",
            "status": "active",
            "created_at": now,
            "capabilities": [],
            "agent_role": "manager",
        }
        # retire-system-token Wave 3: ``g.admin_token`` is no longer
        # a declared global. Still assigned dynamically for tests that
        # read it via ``g.admin_token`` for back-compat.
        g.admin_token = admin_token

        yield base_url, admin_token
    finally:
        server.stop()

        g.connections.clear()
        g.connections.update(snapshot["connections"])
        g.active_agents.clear()
        g.active_agents.update(snapshot["active_agents"])
        # retire-system-token Wave 3: restore admin_token only if a
        # prior caller had set the dynamic attribute; otherwise drop
        # the attribute to leave the module clean.
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
        g.global_vss_load_tested = snapshot["global_vss_load_tested"]
        g.global_vss_load_successful = snapshot["global_vss_load_successful"]
        _engine.reset_engine_cache()
        g.agent_event_signals.clear()

        httpx.Client.__init__ = original_client_init  # type: ignore[assignment]
        httpx.AsyncClient.__init__ = original_async_init  # type: ignore[assignment]

        for k, v in env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def _read_sse_event(response: httpx.Response, timeout: float = 3.0) -> dict:
    """Read SSE bytes until a `data:` frame arrives + parse as JSON."""

    async def _consume() -> dict:
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise AssertionError("SSE stream closed before any data frame")

    try:
        return await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise AssertionError(
            f"timed out after {timeout}s waiting for an SSE `data:` frame"
        ) from exc


async def test_get_mcp_registers_session_and_runtime_queue(live_server) -> None:
    """Opening GET /mcp inserts an mcp_sessions row + attaches a queue.

    The wiring this pins: the GET /mcp handler resolves the bearer to
    an agent_id via `get_agent_id(token)`, then `register_session` +
    `attach_runtime_queue` — observable via the `mcp_sessions` table
    and `session_registry._runtime_queues`.
    """
    base_url, _ = live_server
    alice_token = _seed_worker("alice")

    # Snapshot baseline: _runtime_queues is process-global state shared by
    # any concurrent test in the same xdist worker, so we measure deltas
    # rather than absolute counts. The mcp_sessions count is per-agent
    # and isolated to this test's "alice".
    baseline_queues = _runtime_queues_size()
    assert _mcp_sessions_count_for("alice") == 0

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0, follow_redirects=True) as client:
        async with client.stream(
            "GET",
            "/mcp",
            headers={
                "Authorization": f"Bearer {alice_token}",
                "Accept": "text/event-stream",
            },
        ) as response:
            assert response.status_code == 200, await response.aread()

            for _ in range(40):
                if _mcp_sessions_count_for("alice") >= 1:
                    break
                await asyncio.sleep(0.05)
            assert _mcp_sessions_count_for("alice") == 1, (
                "GET /mcp must register a session row for the bearer's agent_id"
            )
            assert _runtime_queues_size() >= baseline_queues + 1, (
                "GET /mcp must attach an asyncio queue for runtime fan-out"
            )

    # After the stream closes, the row + queue must be cleaned up.
    for _ in range(60):
        if (
            _mcp_sessions_count_for("alice") == 0
            and _runtime_queues_size() <= baseline_queues
        ):
            break
        await asyncio.sleep(0.05)
    assert _mcp_sessions_count_for("alice") == 0, (
        "Closing GET /mcp must delete the mcp_sessions row"
    )
    assert _runtime_queues_size() <= baseline_queues, (
        "Closing GET /mcp must detach the runtime queue (delta vs baseline)"
    )


async def test_notification_reaches_both_concurrent_get_streams(live_server) -> None:
    """Two GET /mcp subs as alice; a send_agent_message → both receive the
    `notifications/resources/updated` envelope on their SSE stream.

    This is the wire-level proof Candidate A had to deliver. Before the
    transport hook, `fanout_to_agent` iterated an empty `_runtime_queues`
    map and every notification dropped at the wire.
    """
    base_url, admin_token = live_server
    alice_token = _seed_worker("alice")

    async with httpx.AsyncClient(base_url=base_url, timeout=15.0, follow_redirects=True) as client:
        s1 = client.stream(
            "GET",
            "/mcp",
            headers={
                "Authorization": f"Bearer {alice_token}",
                "Accept": "text/event-stream",
            },
        )
        s2 = client.stream(
            "GET",
            "/mcp",
            headers={
                "Authorization": f"Bearer {alice_token}",
                "Accept": "text/event-stream",
            },
        )
        async with s1 as r1, s2 as r2:
            assert r1.status_code == 200
            assert r2.status_code == 200

            for _ in range(60):
                if _mcp_sessions_count_for("alice") >= 2:
                    break
                await asyncio.sleep(0.05)
            assert _mcp_sessions_count_for("alice") == 2, (
                f"expected 2 sessions for alice, got {_mcp_sessions_count_for('alice')}"
            )

            # Trigger a notification by POSTing send_agent_message via /mcp.
            async with httpx.AsyncClient(
                base_url=base_url, timeout=10.0, follow_redirects=True
            ) as poster:
                rpc = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "send_agent_message",
                        "arguments": {
                            "token": admin_token,
                            "recipient_id": "alice",
                            "message": "hello-from-admin",
                            "deliver_method": "store",
                        },
                    },
                }
                # POST /mcp may return inline JSON or SSE — either is fine.
                send_task = asyncio.create_task(
                    poster.post(
                        "/mcp",
                        json=rpc,
                        headers={
                            "Authorization": f"Bearer {admin_token}",
                            "Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json",
                        },
                    )
                )

                # Read concurrently so the second stream isn't waiting
                # through the first one's full timeout window.
                ev1, ev2, post_resp = await asyncio.gather(
                    _read_sse_event(r1, timeout=5.0),
                    _read_sse_event(r2, timeout=5.0),
                    send_task,
                )
                assert post_resp.status_code == 200, post_resp.text

                for ev in (ev1, ev2):
                    assert ev.get("method") == "notifications/resources/updated", ev
                    assert ev.get("params", {}).get("uri") == "agent-mcp://inbox/alice", ev
                    assert ev.get("jsonrpc") == "2.0", ev


async def test_get_mcp_without_bearer_returns_401(live_server) -> None:
    """The middleware still gates /mcp at the HTTP layer — our GET
    handler must not bypass auth."""
    base_url, _ = live_server
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0, follow_redirects=True) as client:
        r = await client.get(
            "/mcp", headers={"Accept": "text/event-stream"}
        )
        assert r.status_code == 401, r.text
