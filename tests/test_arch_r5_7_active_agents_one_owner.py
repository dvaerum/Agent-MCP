"""arch-r5 #7 — one owner for "which agents are active".

Before this PR the invariant "``state.active_agents`` holds exactly the
live (non-terminated, non-tombstone) agent rows" was load-bearing for
auth but enforced only by scattered docstrings. Three independent call
sites answered "is this agent active":

  * ``app.main_app._bearer_is_active`` — cache-only, token-keyed (the
    ``/mcp`` auth gate's source of truth).
  * ``tools.admin_tools.view_status_tool_impl`` — direct scan of
    ``g.active_agents.items()``.
  * ``tools.agent_communication_tools._agents_active_by_id`` — routed
    through ``agent_repo.list_active()``, a FRESH DB QUERY, not the
    cache.

If the cache and the DB ever disagreed (a terminate that evicts the
cache vs. a warm path re-adding a stale row) auth and ``view_status``
could report different active sets and nothing caught it. This test
drives an agent through create -> terminate -> restore (warm) ->
terminate -> purge and asserts all three call sites agree at every
step. ``_agents_active_by_id`` now delegates to
``AgentRepository.active_agent_ids()`` — the single owner, itself a
projection of the SAME cache ``_bearer_is_active`` and ``view_status``
read — so the three can no longer drift.
"""

from __future__ import annotations

import json

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


async def _status_active_ids(admin) -> set[str]:
    """Parse the agent_ids ``view_status`` reports as active."""
    result = await admin.assert_tool_succeeds("view_status", {})
    text = result[0].text
    payload = json.loads(text.split("MCP Server Status:\n", 1)[1])
    return set(payload["agents_details"].keys())


async def _assert_agrees(
    admin, token: str, agent_id: str, *, expected: bool, step: str,
) -> None:
    from agent_mcp.app.main_app import _bearer_is_active
    from agent_mcp.tools.agent_communication_tools import (
        _agents_active_by_id,
    )

    bearer_active = _bearer_is_active(token)
    comm_active = agent_id in _agents_active_by_id()
    status_active = agent_id in await _status_active_ids(admin)

    assert bearer_active is expected, (
        f"[{step}] _bearer_is_active disagreed: {bearer_active!r} "
        f"(expected {expected!r})"
    )
    assert comm_active is expected, (
        f"[{step}] _agents_active_by_id disagreed: {comm_active!r} "
        f"(expected {expected!r})"
    )
    assert status_active is expected, (
        f"[{step}] view_status disagreed: {status_active!r} "
        f"(expected {expected!r})"
    )


async def test_active_set_agrees(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        # create_worker seeds both the DB row and the cache => active
        # on every surface.
        await _assert_agrees(
            admin, alice.token, "alice", expected=True, step="created",
        )

        # terminate: cache eviction + DB status flip.
        term = await admin.call("terminate_agent", {"agent_id": "alice"})
        assert "terminated" in term[0].text.lower(), term[0].text
        await _assert_agrees(
            admin, alice.token, "alice", expected=False, step="terminated",
        )

        # restore (warm): repopulates the cache under the SAME token.
        restore = await admin.call("restore_agent", {"agent_id": "alice"})
        assert "restored" in restore[0].text.lower(), restore[0].text
        await _assert_agrees(
            admin, alice.token, "alice", expected=True, step="restored",
        )

        # terminate again, then purge (hard-delete + tombstone).
        term2 = await admin.call("terminate_agent", {"agent_id": "alice"})
        assert "terminated" in term2[0].text.lower(), term2[0].text
        purge = await admin.call("purge_agent", {"agent_id": "alice"})
        assert "purged" in purge[0].text.lower(), purge[0].text
        await _assert_agrees(
            admin, alice.token, "alice", expected=False, step="purged",
        )
