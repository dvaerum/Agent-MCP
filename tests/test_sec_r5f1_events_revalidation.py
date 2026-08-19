"""Security regression (R5-F1) for the operator dashboard live-update SSE
channel (``app/routers/events.py``, ``GET /api/events``).

The stream authenticated its operator ONCE at connect
(``require_operator_session`` via ``Depends``) and then pumped
``notifications/resources/updated`` hints forever with zero re-check: no
TTL, no periodic re-auth, no liveness gate on the loop. A revoked/logged-
out session (or one whose project membership was pulled) kept receiving
live mutation notifications indefinitely — the SAME bug class already
fixed on three sibling long-lived channels:

  * ``app/routers/delivery.py`` — ``REVALIDATE_SECONDS`` loop calling
    ``is_active_agent`` before every emit (R13-F2).
  * ``tools/agent_communication_tools.py`` ``wait_for_events`` — re-checks
    termination every ~2s slice.
  * ``app/main_app.py`` ``_pump`` (GET ``/mcp``) — ``_bearer_is_active``
    re-check before every emit (AC-R29-1).

This file pins the same contract on ``events.py``: an open stream must
tear down within one ``REVALIDATE_SECONDS`` tick of (a) the caller's
session being logged out, or (b) the caller's project membership being
pulled — and a still-valid session must keep receiving events
uninterrupted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_mcp.features import operator_events
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ── router.db + project-registry env (mirrors test_sec_backend_session_authz.py) ──


@pytest.fixture
def router_env(tmp_path: Path, monkeypatch):
    """Wire a tmp router.db + project registry mapping project "beta" to
    the workspace ``mcp_session`` will create (``tmp_path / "project"``),
    so the backend's ``_backend_project_name()`` reverse-map resolves and
    the dep's real project-membership authorization path is exercised
    (not the ad-hoc/no-registry fallback)."""
    from agent_mcp.router import identity
    from agent_mcp.router import project_registry as _pr

    router_db = tmp_path / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(router_db))
    identity.run_router_migrations_upgrade()

    project_dir = tmp_path / "project"  # same dir mcp_session() creates
    registry_file = tmp_path / "projects.local.json"
    registry_file.write_text(
        json.dumps({"beta": {"workspace": str(project_dir)}})
    )
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(registry_file))
    monkeypatch.setattr(_pr, "REGISTRY_PATH", registry_file, raising=False)

    # Consume the first-user sysadmin bootstrap so our test user is a
    # plain non-sysadmin operator with only the membership we grant.
    identity.create_user(username="seed-sysadmin", password="pw")

    return identity


def _make_member(
    identity, *, username: str, role: str = "operator", project: str = "beta"
) -> tuple[str, str]:
    """Create a user with ``role`` membership in ``project``; return
    ``(user_id, session_id)`` for a freshly minted, live session."""
    uid = identity.create_user(username=username, password="pw")
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project, uid, role),
        )
    session_id = identity.create_session(uid)
    return uid, session_id


# ── raw-ASGI stream driver (mirrors test_delivery_bearer_liveness.py) ──


def _get_scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
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


def _cookie_scope(session_id: str) -> dict[str, Any]:
    return _get_scope(
        "/api/events",
        [
            (b"accept", b"text/event-stream"),
            (b"cookie", f"agent_mcp_session={session_id}".encode()),
        ],
    )


class _StreamDriver:
    """Drives an open SSE app call as a background task, exposing the
    frames sent so far and a way to hang up."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.started = asyncio.Event()
        self._release = asyncio.Event()

    async def receive(self) -> dict[str, Any]:
        await self._release.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message["type"] == "http.response.start":
            self.started.set()

    def disconnect(self) -> None:
        self._release.set()

    def data_frames(self) -> list[dict]:
        out = []
        for m in self.sent:
            if m["type"] != "http.response.body":
                continue
            body = m["body"].decode()
            for line in body.splitlines():
                if line.startswith("data: "):
                    out.append(json.loads(line[len("data: "):]))
        return out


async def _open(app, scope: dict[str, Any]) -> tuple[asyncio.Task, _StreamDriver]:
    driver = _StreamDriver()
    task = asyncio.create_task(app(scope, driver.receive, driver.send))
    await asyncio.wait_for(driver.started.wait(), timeout=5.0)
    assert driver.sent[0]["status"] == 200, driver.sent[0]
    return task, driver


# ── tests ────────────────────────────────────────────────────────────


async def test_still_valid_session_keeps_receiving_events(
    tmp_path: Path, router_env, monkeypatch
):
    """Happy path: a normal, still-valid session keeps receiving events
    uninterrupted across multiple revalidation ticks."""
    from agent_mcp.app.routers import events

    monkeypatch.setattr(events, "REVALIDATE_SECONDS", 0.05)

    _, session_id = _make_member(router_env, username="alice")

    async with mcp_session(tmp_path) as admin:
        app = admin.client.app
        task, driver = await _open(app, _cookie_scope(session_id))
        try:
            # Give a few revalidation ticks a chance to run (and NOT
            # tear the stream down) before we publish.
            await asyncio.sleep(0.2)
            assert not task.done(), "still-valid session's stream ended early"

            operator_events.publish({"marker": "still-alive"})
            # Poll for the frame to land (bounded by REVALIDATE_SECONDS).
            for _ in range(50):
                if any(
                    f.get("marker") == "still-alive"
                    for f in driver.data_frames()
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("valid session never received the event")
            assert not task.done(), "still-valid session's stream ended"
        finally:
            driver.disconnect()
            if not task.done():
                task.cancel()


async def test_stream_torn_down_after_logout(
    tmp_path: Path, router_env, monkeypatch
):
    """An OPEN /api/events stream must stop delivering after the caller
    logs out mid-stream (session row deleted), within one revalidation
    tick — not survive indefinitely."""
    from agent_mcp.app.routers import events
    from agent_mcp.router import identity

    monkeypatch.setattr(events, "REVALIDATE_SECONDS", 0.05)

    _, session_id = _make_member(router_env, username="bob")

    async with mcp_session(tmp_path) as admin:
        app = admin.client.app
        task, driver = await _open(app, _cookie_scope(session_id))
        before = operator_events.subscriber_count()
        try:
            # Simulate POST /logout: drop the session row.
            identity.delete_session(session_id)
            # Confirm it's really dead per the source of truth.
            assert identity.get_session(session_id) is None

            # A mutation fires while the (now-revoked) stream is still
            # technically open.
            operator_events.publish({"marker": "post-logout"})

            await asyncio.wait_for(task, timeout=5.0)
        finally:
            driver.disconnect()
            if not task.done():
                task.cancel()

        assert not any(
            f.get("marker") == "post-logout" for f in driver.data_frames()
        ), "revoked session still received a post-logout notification"
        assert operator_events.subscriber_count() == before - 1, (
            "stream's finally never ran — subscriber leaked after logout "
            "teardown"
        )


async def test_stream_torn_down_after_membership_removed(
    tmp_path: Path, router_env, monkeypatch
):
    """An OPEN /api/events stream must stop delivering once the caller's
    project membership is pulled mid-stream, within one revalidation
    tick."""
    from agent_mcp.app.routers import events

    monkeypatch.setattr(events, "REVALIDATE_SECONDS", 0.05)

    uid, session_id = _make_member(router_env, username="carol")

    async with mcp_session(tmp_path) as admin:
        app = admin.client.app
        task, driver = await _open(app, _cookie_scope(session_id))
        before = operator_events.subscriber_count()
        try:
            # Simulate the membership being pulled (e.g. removed from
            # the project, or downgraded away, by an admin).
            with router_env._connect() as conn:
                conn.execute(
                    "DELETE FROM project_membership "
                    "WHERE user_id = ? AND project_name = ?",
                    (uid, "beta"),
                )

            operator_events.publish({"marker": "post-membership-removal"})

            await asyncio.wait_for(task, timeout=5.0)
        finally:
            driver.disconnect()
            if not task.done():
                task.cancel()

        assert not any(
            f.get("marker") == "post-membership-removal"
            for f in driver.data_frames()
        ), (
            "session with revoked project membership still received a "
            "notification"
        )
        assert operator_events.subscriber_count() == before - 1, (
            "stream's finally never ran — subscriber leaked after "
            "membership-removal teardown"
        )


async def test_invalid_session_never_opens_stream(tmp_path: Path, router_env):
    """Sanity: an unknown/garbage session cookie still 401s at open (the
    revalidation fix must not weaken the initial gate)."""
    async with mcp_session(tmp_path) as admin:
        app = admin.client.app
        driver = _StreamDriver()
        task = asyncio.create_task(
            app(_cookie_scope("not-a-real-session"), driver.receive, driver.send)
        )
        await asyncio.wait_for(driver.started.wait(), timeout=5.0)
        assert driver.sent[0]["status"] == 401
        driver.disconnect()
        await asyncio.wait_for(task, timeout=5.0)
