"""Security regression (R13-F2 / R13-F3) for the ADR-0021 delivery
transport (``app/routers/delivery.py``).

R13-F2 — terminated/tombstone bearer must NOT authenticate. The old
``require_agent_bearer`` resolved identity via ``get_agent_id`` →
``get_by_token``, which returns the row for ANY status (its own docstring
says "NOT an auth gate"). A terminated (or reserved ``__tombstone_*``)
bearer therefore still passed on ``/delivery/status`` and
``/delivery/stream`` even though it 401s on ``/mcp``. This is the
SEC-A/B / AC-R29-1 liveness-vs-existence class, re-introduced on the new
delivery path (commit e5d9434). The gate must reject unless the agent is
LIVE (canonical ``LIVE_AGENT_SQL`` — excludes ``terminated`` AND
``tombstone``), and an in-flight stream must tear down on mid-stream
revocation (matching the GET /mcp pump at ``main_app.py:1336``).

R13-F3 — a non-dict JSON body must 4xx, not 500. The route used raw
``request.json()`` then ``(body or {}).get("status")``; a truthy non-dict
JSON value (``[1,2,3]``, ``42``, ``"idle"``, ``true``) survives ``or {}``,
has no ``.get`` → ``AttributeError`` → unhandled 500. The canonical
``utils/json_utils.get_sanitized_json_body`` object-guard (used by every
other ``app/routers/`` body route) turns that into a clean 4xx.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from agent_mcp.features import delivery_transport as dt
from tests.harness import mcp_session


@pytest.fixture(autouse=True)
def _clear_registry():
    dt.clear()
    yield
    dt.clear()


# ── helpers ─────────────────────────────────────────────────────────


def _terminate(agent_id: str, token: str) -> None:
    """Terminate ``agent_id`` the way the app does: flip the DB row to
    ``terminated`` (the auth source of truth) and evict the bearer from
    the in-memory cache."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE agents SET status='terminated', terminated_at=? "
            "WHERE agent_id=?",
            (now, agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    g.active_agents.pop(token, None)


def _insert_tombstone(agent_id: str, token: str) -> None:
    """Insert a ``status='tombstone'`` row bound to a reserved
    ``__tombstone_*`` token (mirrors ``insert_tombstone``)."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO agents "
            "(token, agent_id, created_at, status, "
            " working_directory, color, updated_at) "
            "VALUES (?, ?, ?, 'tombstone', '', '#000000', ?)",
            (token, agent_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _get_scope(
    path: str, headers: list[tuple[bytes, bytes]]
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), *headers],
        "client": ("127.0.0.1", 45678),
        "server": ("testserver", 80),
    }


async def _open_stream_status(app, scope: dict[str, Any]) -> int:
    """Drive ``app`` for ``scope``, hanging up as soon as it responds;
    return the HTTP status of the response start.

    A 401 (dependency rejects) is a plain JSONResponse; a 200 opens the
    EventSourceResponse. Either way we disconnect on first send so an
    open stream doesn't block the test."""
    sent: list[dict] = []
    responded = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await responded.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        responded.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=10.0)
    assert sent and sent[0]["type"] == "http.response.start", (
        f"no response start: {sent!r}"
    )
    return sent[0]["status"]


# ── R13-F2: liveness gate ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminated_bearer_rejected_on_status(tmp_path: Path):
    """A terminated worker's bearer must 401 on POST /delivery/status."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _terminate(alice.agent_id, alice.token)
        r = admin.client.post(
            "/api/delivery/status",
            headers={"Authorization": f"Bearer {alice.token}"},
            json={"status": "working"},
        )
        assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_terminated_bearer_rejected_on_stream(tmp_path: Path):
    """A terminated worker's bearer must 401 on GET /delivery/stream."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _terminate(alice.agent_id, alice.token)
        status = await _open_stream_status(
            admin.client.app,
            _get_scope(
                "/api/delivery/stream",
                [
                    (b"accept", b"text/event-stream"),
                    (b"authorization", f"Bearer {alice.token}".encode()),
                ],
            ),
        )
        assert status == 401


@pytest.mark.asyncio
async def test_tombstone_token_rejected_on_status(tmp_path: Path):
    """A reserved ``__tombstone_*`` token must not resolve to a live
    gate (secondary sibling)."""
    async with mcp_session(tmp_path) as admin:
        tomb_id = "ghost"
        tomb_token = f"__tombstone_{tomb_id}"
        _insert_tombstone(tomb_id, tomb_token)
        r = admin.client.post(
            "/api/delivery/status",
            headers={"Authorization": f"Bearer {tomb_token}"},
            json={"status": "working"},
        )
        assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_inflight_stream_torn_down_on_revocation(
    tmp_path: Path, monkeypatch
):
    """An OPEN delivery stream must tear down when the bearer is revoked
    mid-stream (periodic re-validation), not only refuse at open — the
    contract the GET /mcp pump enforces at main_app.py:1336."""
    from agent_mcp.app.routers import delivery

    # Tight re-validation cadence so the test doesn't wait on the ping.
    monkeypatch.setattr(delivery, "REVALIDATE_SECONDS", 0.05)

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        app = admin.client.app
        scope = _get_scope(
            "/api/delivery/stream",
            [
                (b"accept", b"text/event-stream"),
                (b"authorization", f"Bearer {alice.token}".encode()),
            ],
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def receive() -> dict[str, Any]:
            await release.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                started.set()

        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(started.wait(), timeout=5.0)
            assert dt.is_connected(alice.agent_id) is True

            # Revoke mid-stream: the open generator must notice and end.
            _terminate(alice.agent_id, alice.token)
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            release.set()
            if not task.done():
                task.cancel()

        assert dt.is_connected(alice.agent_id) is False, (
            "revoked stream left the transport registered — the "
            "generator's finally never ran"
        )


# ── R13-F3: non-dict body guard ─────────────────────────────────────


@pytest.mark.parametrize("bad_body", ["[1,2,3]", "42", '"idle"', "true"])
@pytest.mark.asyncio
async def test_nondict_body_returns_4xx_not_500(tmp_path: Path, bad_body):
    """A truthy non-dict JSON body must 4xx (clean), never 500."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        r = admin.client.post(
            "/api/delivery/status",
            headers={
                "Authorization": f"Bearer {alice.token}",
                "Content-Type": "application/json",
            },
            content=bad_body,
        )
        assert 400 <= r.status_code < 500, (
            f"non-dict body {bad_body!r} → {r.status_code} (want 4xx): "
            f"{r.text}"
        )


# ── happy path (must keep working) ──────────────────────────────────


@pytest.mark.asyncio
async def test_live_bearer_status_still_records(tmp_path: Path):
    """A LIVE worker still records status and 200s."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        r = admin.client.post(
            "/api/delivery/status",
            headers={"Authorization": f"Bearer {alice.token}"},
            json={"status": "working"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {
            "ok": True, "agent_id": alice.agent_id, "status": "working",
        }
        assert dt.get_status(alice.agent_id) == "working"


@pytest.mark.asyncio
async def test_live_bearer_stream_opens(tmp_path: Path):
    """A LIVE worker's stream opens (200)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        status = await _open_stream_status(
            admin.client.app,
            _get_scope(
                "/api/delivery/stream",
                [
                    (b"accept", b"text/event-stream"),
                    (b"authorization", f"Bearer {alice.token}".encode()),
                ],
            ),
        )
        assert status == 200
