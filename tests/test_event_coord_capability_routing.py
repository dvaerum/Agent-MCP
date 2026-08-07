"""Tests for `unassigned_task_appeared` wake-up fan-out.

PR5 RETIRED the structured capability-tag routing. There used to be a
`req ⊆ caps` subset match between a task's `required_capabilities` and an
agent's `capabilities` tag list — but that filter was already a no-op
(an empty required set matched everyone, and no project ever populated
the tags). Both columns are physically dropped.

The behaviour now: EVERY unassigned task wakes EVERY active, non-admin
agent with a skinny `unassigned_task_appeared` event — no capability
gating, no `required_capabilities` in the payload. These tests pin that
"notify everyone regardless" contract (the file previously exercised the
subset match this replaces).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.harness import with_bearer

pytestmark = pytest.mark.asyncio


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


async def test_unassigned_task_wakes_every_active_agent(tmp_path: Path) -> None:
    """A created unassigned task wakes every active agent, regardless of
    any (now-removed) capability tags."""
    from agent_mcp.tools.task_tools import assign_task_tool_impl
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("worker-a")
        worker_b = await admin.create_worker("worker-b")

        async def waiter(session):
            return await session.call(
                "wait_for_events", {"timeout_seconds": 3}
            )

        a_task = asyncio.create_task(waiter(worker_a))
        b_task = asyncio.create_task(waiter(worker_b))
        await asyncio.sleep(0.2)

        with with_bearer(admin.admin_token):
            await assign_task_tool_impl({
                "token": admin.admin_token,
                "task_title": "Build the API",
                "task_description": "REST endpoints for /widgets.",
            })

        a_blocks = await asyncio.wait_for(a_task, timeout=5.0)
        b_blocks = await asyncio.wait_for(b_task, timeout=5.0)

        for blocks, who in ((a_blocks, "worker-a"), (b_blocks, "worker-b")):
            body = json.loads(_content_text(blocks))
            unassigned = [
                e for e in body.get("events", [])
                if e.get("type") == "unassigned_task_appeared"
            ]
            assert unassigned, (
                f"{who} should see unassigned_task_appeared; got {body}"
            )
            payload = unassigned[0]["payload"]
            # Skinny payload: title + priority, NO description, and NO
            # required_capabilities (the tag routing is retired).
            assert "description" not in payload, (
                f"unassigned_task_appeared must be skinny; got {payload}"
            )
            assert "required_capabilities" not in payload, (
                f"required_capabilities must be gone; got {payload}"
            )
            assert payload["title"] == "Build the API"


async def test_second_unassigned_task_also_wakes_everyone(
    tmp_path: Path,
) -> None:
    """There is no per-agent capability gate: a second unassigned task
    (which previously might have been filtered by tags) still reaches
    every agent."""
    from agent_mcp.tools.task_tools import assign_task_tool_impl
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        generalist = await admin.create_worker("generalist")

        async def waiter():
            return await generalist.call(
                "wait_for_events", {"timeout_seconds": 3}
            )

        w_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)
        with with_bearer(admin.admin_token):
            await assign_task_tool_impl({
                "token": admin.admin_token,
                "task_title": "Formerly-backend-only task",
                "task_description": "Anyone can do this now.",
            })
        blocks = await asyncio.wait_for(w_task, timeout=5.0)
        body = json.loads(_content_text(blocks))
        unassigned = [
            e for e in body.get("events", [])
            if e.get("type") == "unassigned_task_appeared"
        ]
        assert unassigned, (
            f"generalist should wake for any unassigned task; got {body}"
        )
