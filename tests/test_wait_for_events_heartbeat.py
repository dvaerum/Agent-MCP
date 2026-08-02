"""Behavioral tests for the event-loop heartbeat long-hold.

These exercise ``wait_for_events_tool_impl`` end-to-end across the hold-
strategy resolution + MCP progress-notification path. The pytest harness
drives the tool handler directly (no ASGI), so there is no live MCP
``request_ctx`` — we install a stub RequestContext (recording session +
``_meta.progressToken``) into the SDK's ContextVar to simulate a real
on-wire call, exactly as the SDK would before dispatch.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json

import pytest

import agent_mcp.tools.agent_communication_tools as acm
import agent_mcp.core.client_hold_strategy as chs
from agent_mcp.core import client_info_registry
from mcp.server.lowlevel.server import request_ctx

pytestmark = pytest.mark.asyncio


# --- stub MCP request context ---------------------------------------------


class _RecordingSession:
    def __init__(self):
        self.progress_calls: list[dict] = []

    async def send_progress_notification(
        self,
        *,
        progress_token,
        progress,
        total=None,
        message=None,
        related_request_id=None,
    ):
        self.progress_calls.append(
            {
                "token": progress_token,
                "progress": progress,
                "related_request_id": related_request_id,
            }
        )


class _BrokenSession:
    """A session whose transport is gone: every progress send raises,
    exactly as aiohttp does when the client TCP connection has closed
    (``ClientConnectionResetError``). ``send_progress_heartbeat`` swallows
    the raise and returns False — the signal the hold loop must act on."""

    def __init__(self):
        self.attempts = 0

    async def send_progress_notification(self, **_kwargs):
        self.attempts += 1
        raise ConnectionResetError("Cannot write to closing transport")


class _FlakySession:
    """Fails its first progress send, then succeeds — a one-off transient
    blip on an otherwise-live transport. Must NOT trip the reaper."""

    def __init__(self):
        self.attempts = 0

    async def send_progress_notification(self, **_kwargs):
        self.attempts += 1
        if self.attempts == 1:
            raise ConnectionResetError("transient")
        # otherwise succeed


class _Meta:
    def __init__(self, token):
        self.progressToken = token


class _Ctx:
    def __init__(self, token, session):
        self.meta = _Meta(token)
        self.session = session
        self.request_id = "req-test-1"


@contextlib.contextmanager
def _install_request_ctx(progress_token):
    session = _RecordingSession()
    tok = request_ctx.set(_Ctx(progress_token, session))
    try:
        yield session
    finally:
        request_ctx.reset(tok)


def _parse(result) -> dict:
    """Render a ToolResult to its wire text and parse the JSON envelope."""
    from agent_mcp.core.tool_result import render_as_text_content

    return json.loads(render_as_text_content(result)[0].text)


def _future_since() -> str:
    return (_dt.datetime.now() + _dt.timedelta(seconds=3600)).isoformat()


@pytest.fixture(autouse=True)
def _clear_client_info():
    client_info_registry.clear()
    yield
    client_info_registry.clear()


# ---------------------------------------------------------------------------


async def test_known_heartbeat_client_emits_progress_during_hold(
    tmp_path, monkeypatch
):
    """A claude-code agent holds and emits periodic progress heartbeats
    while there are no events, then returns an empty envelope."""
    from tests.harness import mcp_session

    # Fire heartbeats fast so the test doesn't wait 25s.
    monkeypatch.setattr(chs, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(acm, "_FLAG_RECHECK_INTERVAL_SECONDS", 0.05)

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        client_info_registry.record_client_info(alice.agent_id, "claude-code", "2.1.207")

        with _install_request_ctx("tok-123") as session:
            result = await acm.wait_for_events_tool_impl(
                {"since": _future_since(), "timeout_seconds": 1},
                principal=alice._principal(),
            )

        env = _parse(result)
        assert env["events"] == []
        assert len(session.progress_calls) >= 2, (
            f"expected periodic heartbeats; got {session.progress_calls}"
        )
        # progressToken threaded through; progress monotonically increasing.
        assert all(c["token"] == "tok-123" for c in session.progress_calls)
        progresses = [c["progress"] for c in session.progress_calls]
        assert progresses == sorted(progresses)
        assert progresses[0] < progresses[-1]


async def test_heartbeat_client_returns_event_immediately(tmp_path):
    """Even for a heartbeat client, a pending event returns via the fast
    path without holding or emitting heartbeats."""
    from tests.harness import mcp_session, with_bearer
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        client_info_registry.record_client_info(alice.agent_id, "claude-code")
        since = (_dt.datetime.now() - _dt.timedelta(seconds=1)).isoformat()

        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": alice.agent_id,
                    "message": "hi alice",
                    "deliver_method": "store",
                }
            )

        with _install_request_ctx("tok-xyz") as session:
            result = await acm.wait_for_events_tool_impl(
                {"since": since, "timeout_seconds": 30},
                principal=alice._principal(),
            )

        env = _parse(result)
        assert env["events"], f"expected the pending message; got {env}"
        assert session.progress_calls == [], (
            "fast-path return must not emit heartbeats"
        )


async def test_in_table_no_heartbeat_client_never_heartbeats_despite_token(
    tmp_path, monkeypatch
):
    """Cursor is in the identity table as no-heartbeat. Even though it
    sends a progressToken (the false-positive it's famous for), the server
    must NOT emit heartbeats — identity pinning overrides feature-detect."""
    from tests.harness import mcp_session

    monkeypatch.setattr(chs, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(acm, "_FLAG_RECHECK_INTERVAL_SECONDS", 0.05)

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        client_info_registry.record_client_info(alice.agent_id, "cursor", "1.0")

        with _install_request_ctx("cursor-token") as session:
            result = await acm.wait_for_events_tool_impl(
                {"since": _future_since(), "timeout_seconds": 1},
                principal=alice._principal(),
            )

        env = _parse(result)
        assert env["events"] == []
        assert session.progress_calls == [], (
            f"cursor must never receive heartbeats; got {session.progress_calls}"
        )


async def test_dead_client_transport_is_reaped_not_held(tmp_path, monkeypatch):
    """A heartbeat client whose transport has died (every progress send
    raises → send_progress_heartbeat returns False) must be REAPED after a
    few consecutive misses, not parked until the timeout/deadline.

    Regression: the hold loop discarded the heartbeat return value, so a
    dropped client (tailnet blip / proxy restart / crash) left the server
    parked-and-listening — wasting the slot and lingering "online" in
    presence — because the supersede path only reaps on a *reconnect*, and
    a vanished client never reconnects. RED before the fix (held to the
    2s timeout, dozens of send attempts), GREEN after (reaped in ~2 misses).
    """
    from tests.harness import mcp_session

    monkeypatch.setattr(chs, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(acm, "_FLAG_RECHECK_INTERVAL_SECONDS", 0.05)

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        client_info_registry.record_client_info(
            alice.agent_id, "claude-code", "2.1.207"
        )

        session = _BrokenSession()
        tok = request_ctx.set(_Ctx("tok-dead", session))
        loop_start = _dt.datetime.now()
        try:
            result = await acm.wait_for_events_tool_impl(
                # Long timeout: only prompt reaping (not the timeout) can
                # end this hold quickly.
                {"since": _future_since(), "timeout_seconds": 2},
                principal=alice._principal(),
            )
        finally:
            request_ctx.reset(tok)
        elapsed = (_dt.datetime.now() - loop_start).total_seconds()

        env = _parse(result)
        assert env["events"] == []
        assert elapsed < 1.5, (
            f"a dead client must be reaped promptly, not held to the 2s "
            f"timeout; took {elapsed:.2f}s"
        )
        # Stopped after a few misses rather than hammering a dead socket
        # for the whole hold.
        assert 2 <= session.attempts < 6, (
            f"expected reaping after a few missed heartbeats; "
            f"got {session.attempts} attempts"
        )


async def test_transient_heartbeat_blip_does_not_reap(tmp_path, monkeypatch):
    """A single failed heartbeat on an otherwise-live transport must NOT
    reap the hold — the miss counter resets on the next success, so the
    connection keeps parking until its normal timeout. Guards against the
    reaper being too trigger-happy on a one-off blip."""
    from tests.harness import mcp_session

    monkeypatch.setattr(chs, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(acm, "_FLAG_RECHECK_INTERVAL_SECONDS", 0.05)

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        client_info_registry.record_client_info(
            alice.agent_id, "claude-code", "2.1.207"
        )

        session = _FlakySession()
        tok = request_ctx.set(_Ctx("tok-flaky", session))
        loop_start = _dt.datetime.now()
        try:
            result = await acm.wait_for_events_tool_impl(
                {"since": _future_since(), "timeout_seconds": 1},
                principal=alice._principal(),
            )
        finally:
            request_ctx.reset(tok)
        elapsed = (_dt.datetime.now() - loop_start).total_seconds()

        env = _parse(result)
        assert env["events"] == []
        # Held to the ~1s timeout rather than reaping on the single blip.
        assert elapsed >= 0.8, (
            f"a transient blip must not reap the hold; ended in {elapsed:.2f}s"
        )
        # Kept heart-beating well past the miss threshold (blip recovered).
        assert session.attempts > acm._MAX_HEARTBEAT_MISSES


async def test_hold_cap_recycles_returns_empty(tmp_path, monkeypatch):
    """A heartbeat client with a per-connection cap recycles at the cap:
    the hold ends with a clean empty envelope (agent then reconnects),
    rather than holding forever."""
    from tests.harness import mcp_session

    monkeypatch.setattr(chs, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(acm, "_FLAG_RECHECK_INTERVAL_SECONDS", 0.05)
    # Tiny cap so we can observe the recycle without a 24h wait.
    monkeypatch.setattr(
        chs,
        "resolve_hold_strategy",
        lambda *a, **k: chs.HoldStrategy(heartbeat=True, hold_cap=0.4),
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        client_info_registry.record_client_info(alice.agent_id, "claude-code")

        loop_start = _dt.datetime.now()
        with _install_request_ctx("tok-cap") as session:
            # No timeout_seconds: the cap alone must bound the hold.
            result = await acm.wait_for_events_tool_impl(
                {"since": _future_since()},
                principal=alice._principal(),
            )
        elapsed = (_dt.datetime.now() - loop_start).total_seconds()

        env = _parse(result)
        assert env["events"] == []
        assert elapsed < 3.0, f"cap should have recycled ~0.4s; took {elapsed:.2f}s"
        assert len(session.progress_calls) >= 1
