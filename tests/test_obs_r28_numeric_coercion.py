"""OBS-R28-PF (round-28 hardening parity): request-provided numeric
pagination fields must be int-coerced before they index a list slice.

jsonschema's ``integer`` type admits an integral float
(``{"limit": 2.0}`` validates), so a valid-per-schema call reaches the
tool body carrying a Python ``float``. ``view_tasks`` then evaluated
``tasks_to_display[offset:]`` / ``tasks_to_display[:limit]`` WITHOUT
``int()`` coercion → ``TypeError: slice indices must be integers or
None``. Every SIBLING numeric field (``admin_tools.list_agents``,
``messages`` router) int()-coerces, so this endpoint produced a worse
outcome for the same valid input — and was latent exposure to the
numeric-coercion→500 class if the graceful catch ever moved.

``search_tasks`` had the identical gap on ``max_results`` (integer
schema, sliced at ``candidate_tasks[:max_results]`` /
``scored_results[:max_results]`` with no coercion) — fixed in the same
class-sweep.

RED before the fix (the slice raises → genericized isError, so the
block-count assertion fails); GREEN after coercing to ``int``.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_tasks(admin_id: str, count: int = 5) -> list[str]:
    """Populate ``g.tasks`` directly — the surface view_tasks reads."""
    from agent_mcp.core import globals as g

    ids: list[str] = []
    base = _dt.datetime(2025, 1, 1)
    for i in range(count):
        task_id = f"task_seed{i:02d}{secrets.token_hex(4)}"
        created = (base + _dt.timedelta(minutes=i)).isoformat()
        g.tasks[task_id] = {
            "task_id": task_id,
            "title": f"seed-task-{i:02d}",
            "description": "desc",
            "status": "pending",
            "priority": "medium",
            "assigned_to": None,
            "created_by": admin_id,
            "created_at": created,
            "updated_at": created,
            "parent_task": None,
            "child_tasks": [],
            "depends_on_tasks": [],
            "notes": [],
        }
        ids.append(task_id)
    return ids


def _count_task_blocks(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("ID: task_"))


async def test_view_tasks_integral_float_limit_offset(tmp_path) -> None:
    """``limit=2.0`` / ``offset=1.0`` (valid per the integer schema) must
    page correctly, not raise a slice TypeError."""
    async with mcp_session(tmp_path) as admin:
        _seed_tasks("admin", count=5)

        result = await admin.call(
            "view_tasks",
            {"summary": True, "limit": 2.0, "offset": 1.0},
        )
        out = result[0].text if result else ""

        assert not getattr(admin, "_last_is_error", False), (
            f"view_tasks(limit=2.0, offset=1.0) errored — integral-float "
            f"pagination must be int-coerced before slicing:\n{out}"
        )
        assert _count_task_blocks(out) == 2, (
            f"limit=2.0 must return exactly 2 task blocks; got "
            f"{_count_task_blocks(out)}:\n{out}"
        )
        assert "Total: 5" in out, (
            f"total matching count must still be reported:\n{out}"
        )


async def test_search_tasks_integral_float_max_results(tmp_path) -> None:
    """``search_tasks`` slices ``[:max_results]``; an integral-float
    ``max_results=2.0`` must not raise a slice TypeError."""
    async with mcp_session(tmp_path) as admin:
        _seed_tasks("admin", count=5)

        result = await admin.call(
            "search_tasks",
            {"search_query": "seed", "max_results": 2.0},
        )
        out = result[0].text if result else ""

        assert not getattr(admin, "_last_is_error", False), (
            f"search_tasks(max_results=2.0) errored — integral-float "
            f"max_results must be int-coerced before slicing:\n{out}"
        )
