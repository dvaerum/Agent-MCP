"""Delivery-transport registry + endpoint tests (ADR-0021).

The in-process hub (subscribe/push/status) is unit-tested directly; the
two endpoints are tested through the backend app for worker-bearer auth
and status recording. The live SSE frame delivery is a thin mirror of the
proven ``/api/events`` stream — its logic (push → queue) is covered by the
hub tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_mcp.features import delivery_transport as dt
from tests.harness import mcp_session


@pytest.fixture(autouse=True)
def _clear_registry():
    dt.clear()
    yield
    dt.clear()


# ── hub units ───────────────────────────────────────────────────────


def test_push_delivers_to_subscriber():
    sub = dt.subscribe("alice")
    assert dt.is_connected("alice") is True
    assert dt.push("alice", {"kind": "message", "id": "m1"}) == 1
    assert sub.queue.get_nowait() == {"kind": "message", "id": "m1"}


def test_push_with_no_subscriber_drops():
    # No live transport → frame dropped (policy re-fires next cycle).
    assert dt.push("nobody", {"id": "x"}) == 0


def test_unsubscribe_disconnects():
    sub = dt.subscribe("alice")
    dt.unsubscribe(sub)
    assert dt.is_connected("alice") is False
    assert dt.push("alice", {"id": "x"}) == 0


def test_status_set_and_get():
    assert dt.get_status("alice") is None
    dt.set_status("alice", "idle")
    assert dt.get_status("alice") == "idle"


def test_disconnect_does_not_clear_status():
    # A transient drop is not an end — status persists until re-reported.
    sub = dt.subscribe("alice")
    dt.set_status("alice", "idle")
    dt.unsubscribe(sub)
    assert dt.get_status("alice") == "idle"


def test_snapshot_shape():
    dt.subscribe("alice")
    dt.set_status("alice", "working")
    dt.set_status("bob", "dead")  # reported but no live stream
    snap = {row["agent_id"]: row for row in dt.snapshot()}
    assert snap["alice"] == {
        "agent_id": "alice", "connected": True, "streams": 1, "status": "working",
    }
    assert snap["bob"]["connected"] is False
    assert snap["bob"]["status"] == "dead"


# ── endpoints ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_requires_agent_bearer(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/delivery/stream")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_status_requires_agent_bearer(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post("/api/delivery/status", json={"status": "idle"})
        assert r.status_code == 401
        # A bogus bearer also 401s (not resolvable to an agent).
        r2 = admin.client.post(
            "/api/delivery/status",
            headers={"Authorization": "Bearer not-a-real-token"},
            json={"status": "idle"},
        )
        assert r2.status_code == 401


@pytest.mark.asyncio
async def test_status_post_records_transport_status(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        r = admin.client.post(
            "/api/delivery/status",
            headers={"Authorization": f"Bearer {alice.token}"},
            json={"status": "working"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"ok": True, "agent_id": alice.agent_id, "status": "working"}
        assert dt.get_status(alice.agent_id) == "working"


@pytest.mark.asyncio
async def test_status_post_rejects_invalid_status(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        r = admin.client.post(
            "/api/delivery/status",
            headers={"Authorization": f"Bearer {alice.token}"},
            json={"status": "bogus"},
        )
        assert r.status_code == 422
        assert dt.get_status(alice.agent_id) is None
