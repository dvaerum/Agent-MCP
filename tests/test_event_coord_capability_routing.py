"""Tests for capability-routed `unassigned_task_appeared` wake-up.

Spec (PR-2 event-coord): when an unassigned task is created, every
active agent whose `capabilities` satisfies (is a superset of) the
task's `required_capabilities` is woken with a SKINNY
`unassigned_task_appeared` event. Empty required → wake everyone;
empty agent caps → match only empty-required tasks.

PR-1 normalizes capabilities at write time, so the matcher operates
on already-lowercased data. These tests verify the matcher does NOT
double-normalize (which would be a perf wart but not a bug) and that
the subset semantics are exactly as spec'd.
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


async def _set_agent_capabilities(
    agent_id: str, caps: list[str],
) -> None:
    """Direct DB update for test agents created via the harness's
    raw-SQL insert (which seeds an empty capabilities list)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET capabilities = ? WHERE agent_id = ?",
            (json.dumps(caps), agent_id),
        )
        conn.commit()
    finally:
        conn.close()


async def test_subset_match_wakes_only_matching_agent(
    tmp_path: Path,
) -> None:
    """A task requiring ['backend'] wakes worker-backend but not
    worker-frontend."""
    from tests.harness import mcp_session
    from agent_mcp.tools.task_tools import assign_task_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker_backend = await admin.create_worker("worker-backend")
        worker_frontend = await admin.create_worker("worker-frontend")
        await _set_agent_capabilities("worker-backend", ["backend"])
        await _set_agent_capabilities("worker-frontend", ["frontend"])

        async def waiter(session):
            return await session.call(
                "wait_for_events", {"timeout_seconds": 3}
            )

        be_task = asyncio.create_task(waiter(worker_backend))
        fe_task = asyncio.create_task(waiter(worker_frontend))
        await asyncio.sleep(0.2)

        await assign_task_tool_impl({
            "token": admin.admin_token,
            "task_title": "Build the API",
            "task_description": "REST endpoints for /widgets.",
            "required_capabilities": ["backend"],
        })

        be_blocks = await asyncio.wait_for(be_task, timeout=5.0)
        fe_blocks = await asyncio.wait_for(fe_task, timeout=5.0)

        be_body = json.loads(_content_text(be_blocks))
        fe_body = json.loads(_content_text(fe_blocks))

        # worker-backend should have at least one
        # unassigned_task_appeared event.
        be_unassigned = [
            e for e in be_body.get("events", [])
            if e.get("type") == "unassigned_task_appeared"
        ]
        assert be_unassigned, (
            f"worker-backend should see unassigned_task_appeared; "
            f"got {be_body}"
        )
        # Skinny payload: title + priority + required_capabilities,
        # NO description.
        payload = be_unassigned[0]["payload"]
        assert "description" not in payload, (
            f"unassigned_task_appeared must be skinny; got {payload}"
        )
        assert payload["title"] == "Build the API"
        assert payload["required_capabilities"] == ["backend"]

        # worker-frontend should have NO unassigned_task_appeared.
        fe_unassigned = [
            e for e in fe_body.get("events", [])
            if e.get("type") == "unassigned_task_appeared"
        ]
        assert not fe_unassigned, (
            f"worker-frontend should not wake for backend task; "
            f"got {fe_body}"
        )


async def test_empty_required_wakes_everyone(tmp_path: Path) -> None:
    """A task with empty required_capabilities wakes every active
    agent regardless of their capabilities (empty-set is a subset of
    any set)."""
    from tests.harness import mcp_session
    from agent_mcp.tools.task_tools import assign_task_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker_backend = await admin.create_worker("worker-backend")
        worker_frontend = await admin.create_worker("worker-frontend")
        await _set_agent_capabilities("worker-backend", ["backend"])
        await _set_agent_capabilities("worker-frontend", ["frontend"])

        async def waiter(session):
            return await session.call(
                "wait_for_events", {"timeout_seconds": 3}
            )

        be_task = asyncio.create_task(waiter(worker_backend))
        fe_task = asyncio.create_task(waiter(worker_frontend))
        await asyncio.sleep(0.2)

        await assign_task_tool_impl({
            "token": admin.admin_token,
            "task_title": "Open task — any takers?",
            "task_description": "First-come, first-served.",
        })

        be_blocks = await asyncio.wait_for(be_task, timeout=5.0)
        fe_blocks = await asyncio.wait_for(fe_task, timeout=5.0)

        be_body = json.loads(_content_text(be_blocks))
        fe_body = json.loads(_content_text(fe_blocks))

        for body, who in ((be_body, "backend"), (fe_body, "frontend")):
            unassigned = [
                e for e in body.get("events", [])
                if e.get("type") == "unassigned_task_appeared"
            ]
            assert unassigned, (
                f"{who} should wake for empty-required task; "
                f"got {body}"
            )


async def test_empty_agent_caps_only_matches_empty_required(
    tmp_path: Path,
) -> None:
    """An agent with empty capabilities only matches tasks with empty
    required (empty-set is a subset of empty-set; not a superset of
    any non-empty set)."""
    from tests.harness import mcp_session
    from agent_mcp.tools.task_tools import assign_task_tool_impl

    async with mcp_session(tmp_path) as admin:
        generalist = await admin.create_worker("generalist")
        # Leave generalist's capabilities as the default [] from the
        # harness's raw insert.

        async def waiter():
            return await generalist.call(
                "wait_for_events", {"timeout_seconds": 2}
            )

        # Case 1: task requires ["backend"] → generalist should NOT wake.
        w_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)
        await assign_task_tool_impl({
            "token": admin.admin_token,
            "task_title": "Backend-only",
            "task_description": "Needs backend caps.",
            "required_capabilities": ["backend"],
        })
        blocks = await asyncio.wait_for(w_task, timeout=4.0)
        body = json.loads(_content_text(blocks))
        unassigned = [
            e for e in body.get("events", [])
            if e.get("type") == "unassigned_task_appeared"
        ]
        assert not unassigned, (
            f"generalist should not wake for ['backend'] task; "
            f"got {body}"
        )

        # Case 2: task with empty required → generalist SHOULD wake.
        w_task2 = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)
        await assign_task_tool_impl({
            "token": admin.admin_token,
            "task_title": "Open task",
            "task_description": "Anyone can do this.",
        })
        blocks2 = await asyncio.wait_for(w_task2, timeout=4.0)
        body2 = json.loads(_content_text(blocks2))
        unassigned2 = [
            e for e in body2.get("events", [])
            if e.get("type") == "unassigned_task_appeared"
        ]
        assert unassigned2, (
            f"generalist should wake for empty-required task; "
            f"got {body2}"
        )


async def test_lowercase_normalization_idempotent(tmp_path: Path) -> None:
    """The matcher operates on PR-1's already-normalized data.
    Providing mixed-case strings at task-create time should be
    normalized once at write (via the assign_task path's
    normalize_capabilities call) and matched directly thereafter — no
    case sensitivity surprises."""
    from tests.harness import mcp_session
    from agent_mcp.tools.task_tools import assign_task_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("worker")
        # Set capabilities pre-normalized (the harness path bypasses
        # the agent-create normalizer).
        await _set_agent_capabilities("worker", ["backend", "db"])

        async def waiter():
            return await worker.call(
                "wait_for_events", {"timeout_seconds": 3}
            )

        w_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)
        await assign_task_tool_impl({
            "token": admin.admin_token,
            "task_title": "Cap case test",
            "task_description": "Backend + DB.",
            "required_capabilities": ["Backend", "DB"],
        })
        blocks = await asyncio.wait_for(w_task, timeout=5.0)
        body = json.loads(_content_text(blocks))
        unassigned = [
            e for e in body.get("events", [])
            if e.get("type") == "unassigned_task_appeared"
        ]
        assert unassigned, (
            f"worker with ['backend','db'] should match "
            f"required ['Backend','DB']; got {body}"
        )
        # The required_capabilities in the event payload must be
        # lowercase.
        caps = sorted(unassigned[0]["payload"]["required_capabilities"])
        assert caps == ["backend", "db"], (
            f"payload caps should be lowercase; got {caps}"
        )
