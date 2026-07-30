"""Tests for the global + per-agent `auto_event_loop` toggles.

Spec (PR-2 event-coord):
  * On every `wait_for_events` call, check
    `project_settings["config_auto_event_loop_global"]` (ADR-0016) AND
    `agents.auto_event_loop` for the calling agent. If either is OFF,
    return immediately with a single `stop_listening` event.
  * `serverInfo.instructions` wake-loop bootstrap is gated on BOTH
    flags being ON.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


def _set_per_agent_flag(agent_id: str, value: bool) -> None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agents SET auto_event_loop = ? WHERE agent_id = ?",
            (1 if value else 0, agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_global_flag(value: bool) -> None:
    """Insert/update the project_settings row directly to avoid the
    REST roundtrip (which has its own coverage in test_routes_*)."""
    from agent_mcp.db.connection import get_db_connection
    import datetime as _dt

    now = _dt.datetime.now().isoformat()
    raw = "true" if value else "false"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT context_key FROM project_settings "
            "WHERE context_key = ?",
            ("config_auto_event_loop_global",),
        )
        if cur.fetchone():
            cur.execute(
                "UPDATE project_settings SET value = ?, updated_at = ? "
                "WHERE context_key = ?",
                (raw, now, "config_auto_event_loop_global"),
            )
        else:
            cur.execute(
                "INSERT INTO project_settings "
                "(context_key, value, created_at, updated_at, "
                " created_by, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "config_auto_event_loop_global",
                    raw, now, now, "admin", "admin",
                ),
            )
        conn.commit()
    finally:
        conn.close()


async def test_per_agent_off_returns_stop_listening(tmp_path: Path) -> None:
    """When the per-agent flag is OFF, wait_for_events returns
    immediately with a stop_listening event."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_per_agent_flag("alice", False)

        start = asyncio.get_event_loop().time()
        blocks = await alice.call(
            "wait_for_events", {"timeout_seconds": 10}
        )
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 1.0, (
            f"flag-off should return immediately; took {elapsed:.2f}s"
        )
        body = json.loads(_content_text(blocks))
        assert len(body["events"]) == 1
        evt = body["events"][0]
        assert evt["type"] == "stop_listening"
        # Per-agent flag OFF is the operator "Disconnect" — the reason is
        # operator-facing (why + when) so the agent relays it to the human.
        reason = evt["payload"]["reason"].lower()
        assert "paused" in reason or "disconnect" in reason, reason


async def test_global_off_returns_stop_listening(tmp_path: Path) -> None:
    """When the global flag is OFF, wait_for_events returns
    stop_listening for everyone (even if the per-agent flag is ON)."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_global_flag(False)

        start = asyncio.get_event_loop().time()
        blocks = await alice.call(
            "wait_for_events", {"timeout_seconds": 10}
        )
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 1.0, (
            f"global-off should return immediately; took {elapsed:.2f}s"
        )
        body = json.loads(_content_text(blocks))
        evt = body["events"][0]
        assert evt["type"] == "stop_listening"
        assert "config_auto_event_loop_global" in (
            evt["payload"]["reason"]
        )


async def test_server_info_instructions_gated_by_flags(
    tmp_path: Path,
) -> None:
    """The wake-loop bootstrap text is appended only when BOTH flags
    are ON. Wave 6 PR 6: the eligibility chain folds into
    :attr:`Principal.can_wake_loop` at middleware build time; the
    contributor reads that bit directly.
    """
    from tests.harness import mcp_session
    from agent_mcp.app.main_app import _build_principal_from_request

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        # Default: both flags ON → True.
        p = _build_principal_from_request(
            request=None, bearer_token=alice.token, forwarding_operator=None,
        )
        assert p is not None and p.can_wake_loop is True, (
            "default state should enable wake-loop"
        )

        # Per-agent OFF → False.
        _set_per_agent_flag("alice", False)
        p = _build_principal_from_request(
            request=None, bearer_token=alice.token, forwarding_operator=None,
        )
        assert p is not None and p.can_wake_loop is False

        # Per-agent back ON, global OFF → False.
        _set_per_agent_flag("alice", True)
        _set_global_flag(False)
        p = _build_principal_from_request(
            request=None, bearer_token=alice.token, forwarding_operator=None,
        )
        assert p is not None and p.can_wake_loop is False


async def test_admin_bearer_does_not_get_wake_loop(tmp_path: Path) -> None:
    """Admin bearers coordinate; they don't run the worker wake loop.
    The eligibility check returns False for the admin token even with
    both flags ON."""
    from tests.harness import mcp_session
    from agent_mcp.app.main_app import _build_principal_from_request

    async with mcp_session(tmp_path) as admin:
        p = _build_principal_from_request(
            request=None,
            bearer_token=admin.admin_token,
            forwarding_operator=None,
        )
        assert p is not None and p.can_wake_loop is False, (
            "admin bearer should NOT trigger wake-loop injection"
        )
