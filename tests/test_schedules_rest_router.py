"""REST /api/schedules router (dashboard Schedules page, plan §5.5)."""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _create(admin, body):
    return admin.request("POST", "/api/schedules", json=body)


async def test_create_list_update_delete_flow(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        # Create.
        r = _create(admin, {
            "agent_id": "alice", "prompt": "check CI",
            "interval_seconds": 120,
        })
        assert r.status_code == 200, r.text
        did = r.json()["directive"]["directive_id"]

        # List.
        rl = admin.request("GET", "/api/schedules")
        assert rl.status_code == 200, rl.text
        rows = rl.json()["schedules"]
        assert any(s["directive_id"] == did for s in rows)
        row = next(s for s in rows if s["directive_id"] == did)
        assert row["agent_id"] == "alice"
        assert row["enabled"] is True

        # Update (pause via enable toggle).
        ru = admin.request("PUT", f"/api/schedules/{did}",
                           json={"enabled": False})
        assert ru.status_code == 200, ru.text
        assert ru.json()["directive"]["enabled"] is False
        assert ru.json()["directive"]["status"] == "paused"

        # Delete.
        rd = admin.request("DELETE", f"/api/schedules/{did}")
        assert rd.status_code == 200, rd.text
        rows2 = admin.request("GET", "/api/schedules").json()["schedules"]
        assert not any(s["directive_id"] == did for s in rows2)


async def test_create_enforces_floor_guardrail(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _create(admin, {
            "agent_id": "alice", "prompt": "x", "interval_seconds": 5,
        })
        assert r.status_code == 400, r.text


async def test_update_interval_revalidates_floor(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        did = _create(admin, {
            "agent_id": "alice", "prompt": "x", "interval_seconds": 120,
        }).json()["directive"]["directive_id"]
        r = admin.request("PUT", f"/api/schedules/{did}",
                          json={"interval_seconds": 3})
        assert r.status_code == 400, r.text


async def test_update_missing_is_404(tmp_path):
    async with mcp_session(tmp_path) as admin:
        r = admin.request("PUT", "/api/schedules/sd_missing",
                          json={"enabled": False})
        assert r.status_code == 404, r.text


async def test_routes_require_operator_session(tmp_path):
    """No-auth requests to the schedules routes are rejected."""
    async with mcp_session(tmp_path) as admin:
        for method, path in [
            ("GET", "/api/schedules"),
            ("POST", "/api/schedules"),
            ("PUT", "/api/schedules/sd_x"),
            ("DELETE", "/api/schedules/sd_x"),
        ]:
            r = admin.client.request(method, path, json={})
            assert r.status_code in (401, 403), (method, path, r.status_code)
