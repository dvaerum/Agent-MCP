"""E2E: client-identity recording through the REAL router → REAL backend.

Regression guard for the event-loop token-saving hold. The
``wait_for_events`` hold-strategy resolver keys on the client's
``clientInfo.name`` recorded at the MCP ``initialize`` handshake
(``client_info_registry``). If that recording is bypassed, EVERY agent
(including Claude Code) falls to the 55s no-heartbeat re-poll instead of
the parked heartbeat hold — burning a model turn every ~minute.

Unit-level tests exercised ``_maybe_record_client_info`` and the strategy
resolver in isolation, but NOTHING covered the real
router(aiohttp)→backend(uvicorn UDS) path — which is where recording
actually broke (a bare ``/mcp`` proxy hop 307-redirects to ``/mcp/`` at
Starlette's routing layer, before the recording ASGI wrapper runs).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
import uvicorn


def _serve_backend_on_uds(project_dir: str, sock: Path) -> uvicorn.Server:
    from agent_mcp.app.main_app import create_app

    sock.parent.mkdir(parents=True, exist_ok=True)
    app = create_app(project_dir=project_dir)
    server = uvicorn.Server(
        uvicorn.Config(app, uds=str(sock), log_level="error", lifespan="on")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.25)
    return server


@pytest.mark.asyncio
async def test_initialize_through_router_records_client_info(
    aiohttp_client, router_app, router_env, router_module, systemctl_stub, tmp_path
):
    """A real ``initialize`` POST through the router must record the
    caller's ``clientInfo.name`` in the backend's registry — so the
    hold-strategy resolver can hand Claude Code the heartbeat hold."""
    from agent_mcp.core import client_info_registry as reg
    from agent_mcp.core.auth import generate_token
    from agent_mcp.repositories import agent_repo

    name = "recproj"
    projdir = str(tmp_path / "proj")
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    server = _serve_backend_on_uds(projdir, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")

    try:
        tok = generate_token()
        agent_repo.create(
            token=tok, agent_id="rec-probe", working_directory=projdir,
            agent_role="worker",
        )
        reg.clear()

        client = await aiohttp_client(router_app)
        init = {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "2.1.207"},
            },
        }
        resp = await client.post(
            f"/agent-mcp/mcp/{name}",
            data=json.dumps(init),
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert resp.status == 200, f"initialize failed: {resp.status}"

        recorded = reg.get_client_name("rec-probe")
        assert recorded == "claude-code", (
            f"clientInfo.name was NOT recorded through the router path "
            f"(got {recorded!r}) — the token-saving hold will never engage"
        )
    finally:
        server.should_exit = True
        await asyncio.sleep(0.3)
