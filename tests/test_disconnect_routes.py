"""REST adapters for operator Disconnect / Reconnect.

Thin FastAPI routes over the ``admin_tools`` disconnect/reconnect impls
(operator-only via ``require_operator_session``):

  POST /api/agents/<id>/disconnect   POST /api/agents/disconnect-all
  POST /api/agents/<id>/reconnect    POST /api/agents/reconnect-all

The impls' behaviour is covered by ``test_disconnect_agent.py``; these
tests pin the wire contract (status + body shape) the dashboard reads.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _auto_event_loop(agent_id: str) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT auto_event_loop FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["auto_event_loop"])


async def test_disconnect_route_pauses_agent(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        assert _auto_event_loop("alice") == 1

        resp = admin.post("/api/agents/alice/disconnect", json={})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["agent_id"] == "alice"
        assert "closed_streams" in body
        assert _auto_event_loop("alice") == 0


async def test_reconnect_route_resumes_agent(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")
        admin.post("/api/agents/bob/disconnect", json={})
        assert _auto_event_loop("bob") == 0

        resp = admin.post("/api/agents/bob/reconnect", json={})

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True
        assert _auto_event_loop("bob") == 1


async def test_disconnect_route_unknown_agent_404(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        resp = admin.post("/api/agents/ghost/disconnect", json={})
        assert resp.status_code == 404, resp.text


async def test_disconnect_all_route_flips_global(tmp_path) -> None:
    from agent_mcp.tools import access

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("m1")
        assert access._get_config_bool("config_auto_event_loop_global") is True

        resp = admin.post("/api/agents/disconnect-all", json={})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert "closed_streams" in body
        assert (
            access._get_config_bool("config_auto_event_loop_global") is False
        )


async def test_reconnect_all_route_flips_global(tmp_path) -> None:
    from agent_mcp.tools import access

    async with mcp_session(tmp_path) as admin:
        admin.post("/api/agents/disconnect-all", json={})
        assert (
            access._get_config_bool("config_auto_event_loop_global") is False
        )

        resp = admin.post("/api/agents/reconnect-all", json={})

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True
        assert (
            access._get_config_bool("config_auto_event_loop_global") is True
        )
