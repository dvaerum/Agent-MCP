"""view_tasks must support `summary`, `limit`, and `offset` for use in
projects with many tasks.

A bg agent reported `view_tasks` returning 83k+ chars against a project
with ~40 tasks — exceeding claude-code's per-call token cap. This
exercises three new optional knobs (all backward-compatible: existing
callers see the existing response shape):

1. `summary=true` -> per-task block is the minimal projection
   (task_id, title, status, priority, assigned_to). Same output as
   the existing `summary_mode=true` alias.
2. `limit=<N>` -> at most N tasks returned. Response includes a
   `Total: <M>` line so the caller knows there are more.
3. `offset=<N>` -> skip the first N tasks (post-filter, post-sort).
   Pairs with `limit` for pagination.

Backward-compat regression: with no new params, the response shape is
identical to today's (no `Total:` line injected, no behavioral change).

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_tasks(admin_id: str, count: int = 5) -> list[str]:
    """Populate `g.tasks` directly with `count` synthetic task rows.

    view_tasks reads from the in-memory `g.tasks` cache; the dashboard's
    POST /api/tasks route writes to the DB but does NOT update the cache
    (the lifespan loader is what hydrates it on startup). For
    in-process tests we just put rows in the cache directly — that's
    the surface view_tasks_tool_impl actually reads.
    """
    from agent_mcp.core import globals as g

    # ~1.5 KB description per task -> ~2 KB detailed block,
    # ~150 B summary block. Picks numbers far above/below the test
    # threshold to keep the assertions robust.
    desc = (
        "This is a deliberately verbose description of the task so that "
        "the detailed view of the response inflates well past the "
        "summary view. " * 20
    )
    ids: list[str] = []
    base = _dt.datetime(2025, 1, 1)
    for i in range(count):
        task_id = f"task_seed{i:02d}{secrets.token_hex(4)}"
        # Strictly increasing created_at so sort order is deterministic.
        created = (base + _dt.timedelta(minutes=i)).isoformat()
        g.tasks[task_id] = {
            "task_id": task_id,
            "title": f"seed-task-{i:02d}",
            "description": desc,
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


async def _view(admin, arguments: dict) -> str:
    result = await admin.call("view_tasks", arguments)
    return result[0].text


def _count_task_blocks(text: str) -> int:
    # Each task block starts with "ID: task_" (matches the seed prefix
    # the create endpoint generates). Count those lines.
    return sum(1 for line in text.splitlines() if line.startswith("ID: task_"))


# --- 1. summary=true produces dramatically smaller per-task blocks ---


async def test_summary_true_shrinks_per_task_payload(tmp_path) -> None:
    """summary=true -> each task block is the minimal projection.

    With ~1.5 KB descriptions in detailed mode, the difference per
    task is large (~2 KB detailed vs ~150 B summary)."""
    async with mcp_session(tmp_path) as admin:
        _seed_tasks("admin", count=5)

        full = await _view(admin, {})
        short = await _view(admin, {"summary": True})

        # Both responses must include all 5 task blocks.
        assert _count_task_blocks(full) == 5, full
        assert _count_task_blocks(short) == 5, short

        # The summary payload must be at least 5x smaller than detailed.
        assert len(short) * 5 < len(full), (
            f"summary mode didn't shrink the response enough: "
            f"summary={len(short)} chars, detailed={len(full)} chars"
        )

        # And the absolute per-task budget in summary mode is tight.
        assert len(short) < 5 * 600, (
            f"summary response too large: {len(short)} chars (expected "
            f"<3000 for 5 tiny task blocks): {short[:400]}"
        )


# --- 2. limit caps the result list and exposes the total ---


async def test_limit_caps_results_and_reports_total(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed_tasks("admin", count=5)

        out = await _view(admin, {"summary": True, "limit": 2})

        assert _count_task_blocks(out) == 2, (
            f"limit=2 must return exactly 2 task blocks; got "
            f"{_count_task_blocks(out)}:\n{out}"
        )
        assert "Total: 5" in out, (
            f"limit must surface the total matching count so the caller "
            f"knows more pages exist:\n{out}"
        )


# --- 3. offset advances the window ---


async def test_offset_advances_pagination_window(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed_tasks("admin", count=5)

        page1 = await _view(
            admin, {"summary": True, "limit": 2, "offset": 0}
        )
        page2 = await _view(
            admin, {"summary": True, "limit": 2, "offset": 2}
        )

        assert _count_task_blocks(page1) == 2
        assert _count_task_blocks(page2) == 2

        # Pages must be disjoint: no task ID appears in both.
        def _ids(text: str) -> set[str]:
            return {
                line.removeprefix("ID: ").strip()
                for line in text.splitlines()
                if line.startswith("ID: task_")
            }

        overlap = _ids(page1) & _ids(page2)
        assert not overlap, (
            f"offset must produce a disjoint page; overlap={overlap}\n"
            f"--- page1 ---\n{page1}\n--- page2 ---\n{page2}"
        )

        assert "Total: 5" in page1
        assert "Total: 5" in page2


# --- 4. summary + limit combine cleanly ---


async def test_summary_with_limit_combined(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed_tasks("admin", count=5)

        out = await _view(admin, {"summary": True, "limit": 3})

        assert _count_task_blocks(out) == 3
        assert "Total: 5" in out
        assert len(out) < 3 * 600, (
            f"summary+limit response unexpectedly large: {len(out)} chars"
        )


# --- 5. Backward-compat regression: no new params -> existing shape ---


async def test_no_new_params_response_shape_unchanged(tmp_path) -> None:
    """A caller that passes none of the new params must see the exact
    same response shape as before:
      - All matching tasks are rendered (no silent truncation by limit).
      - No `Total:` line injected (that's a new-API-only field).
    """
    async with mcp_session(tmp_path) as admin:
        _seed_tasks("admin", count=5)

        out = await _view(admin, {})

        assert _count_task_blocks(out) == 5
        assert "Total:" not in out, (
            f"existing callers must not see a new `Total:` field "
            f"injected — that would change the response shape:\n{out}"
        )
        assert "matching tasks shown" in out
