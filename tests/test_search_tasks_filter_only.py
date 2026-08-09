"""search_tasks must accept filter-only calls (no search_query).

Bug report (v5.0.21): a worker tried
``search_tasks(status_filter="pending")`` and got
``"Error: search_query is required and cannot be empty."`` — even
though the JSON-schema's other properties (``status_filter``,
``max_results``, ``include_notes``) clearly imply that filter-only
mode should be a first-class call shape.

This test pins the filter-only contract so a future refactor can't
silently re-introduce the early-return.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _result_text(blocks) -> str:
    parts = []
    for b in blocks:
        text = getattr(b, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _seed_task(
    task_id: str,
    title: str,
    status: str,
    assigned_to: str = "admin",
    description: str = "",
) -> None:
    """INSERT a task row directly so the search-impl's `g.tasks` cache
    sees it. The search-impl reads `g.tasks` (in-memory cache); we
    populate both the table (audit) and the cache (queryable)."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    from tests.conftest import existing_root_task_id

    # R15-BL-1: chain under the single root (first seed = root, rest are
    # children); status-only search is unaffected by parentage.
    parent = existing_root_task_id()

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id, title, description, assigned_to, created_by, "
            " status, priority, created_at, updated_at, "
            " parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, ?, ?, 'admin', ?, 'medium', ?, ?, "
            "        ?, '[]', '[]', '[]')",
            (task_id, title, description, assigned_to, status, now, now,
             parent),
        )
        conn.commit()
    finally:
        conn.close()

    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "description": description,
        "assigned_to": assigned_to,
        "status": status,
        "priority": "medium",
        "created_at": now,
        "updated_at": now,
        "notes": [],
    }


async def test_search_tasks_status_filter_only_no_query(tmp_path) -> None:
    """With only status_filter='pending' and no search_query, the
    tool must return tasks (not an error).
    """
    async with mcp_session(tmp_path) as admin:
        _seed_task("t-pending-1", "First pending task", "pending")
        _seed_task("t-pending-2", "Second pending task", "pending")
        _seed_task("t-done", "Completed task", "completed")

        result = await admin.call(
            "search_tasks",
            {"status_filter": "pending"},
        )
        text = _result_text(result)
        assert not getattr(admin, "_last_is_error", False), (
            f"search_tasks(status_filter='pending') returned isError: {text}"
        )
        assert not ("search_query" in text and "required" in text), (
            f"search_tasks must accept filter-only calls; got: {text}"
        )
        assert "t-pending-1" in text and "t-pending-2" in text, (
            f"filter-only call must return matching tasks; got: {text}"
        )
        assert "t-done" not in text, (
            f"completed task must be excluded when status_filter=pending; "
            f"got: {text}"
        )


async def test_search_tasks_no_args_at_all(tmp_path) -> None:
    """With no args (no query, no filter), the tool must respond
    with either a sensible default listing or a clear error mentioning
    that a query or filter is required — but NOT crash.
    """
    async with mcp_session(tmp_path) as admin:
        _seed_task("t-any", "Some task", "pending")

        result = await admin.call("search_tasks", {})
        text = _result_text(result)
        # Either a successful listing, or a clean error message that
        # doesn't blame `search_query` specifically (since filters are
        # also acceptable now). Crashing/empty is not allowed.
        assert text, "search_tasks({}) returned empty response"
        if getattr(admin, "_last_is_error", False):
            # If we error, it must NOT be the old "search_query is
            # required" — that message is what triggered this bug report.
            assert not ("search_query" in text and "required" in text), (
                f"no-args call must not blame search_query; got: {text}"
            )


async def test_search_tasks_with_query_unchanged(tmp_path) -> None:
    """Calls WITH search_query must continue to score + filter on text.
    Regression guard for the filter-only refactor.
    """
    async with mcp_session(tmp_path) as admin:
        _seed_task("t-alpha", "Alpha frobnicator", "pending")
        _seed_task("t-beta", "Beta widget", "pending")

        result = await admin.call(
            "search_tasks",
            {"search_query": "frobnicator"},
        )
        text = _result_text(result)
        assert not getattr(admin, "_last_is_error", False), text
        assert "t-alpha" in text, (
            f"query-based search must find matching task; got: {text}"
        )
        assert "t-beta" not in text, (
            f"query-based search must exclude non-matching task; got: {text}"
        )
