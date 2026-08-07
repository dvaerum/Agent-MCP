"""Test suite for PR-4 of the database review improvements.

Covers items 5 and 6 from the 2026-06-02 review:

  * Item 5 — wrap bulk writes in explicit transactions. The Python
    sqlite3 module's default isolation_level already groups writes
    on a single connection into one transaction; the review's intent
    is that the call sites declare the boundary explicitly and use
    `executemany` where the per-row work is repetitive. We assert
    the observable shape: `_assign_to_existing_tasks` issues exactly
    one COMMIT regardless of how many task_ids are passed.

  * Item 6 — SQLAlchemy event-listener for slow queries (threshold
    100 ms). We monkeypatch `time.monotonic` to inflate the apparent
    duration of a query past the threshold and confirm the logger
    surfaces a warning containing the truncated SQL.
"""

from __future__ import annotations

import contextlib
import logging
from unittest import mock

import pytest

from tests.harness import mcp_session, with_bearer

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Item 5 — bulk assign should commit once, not per-task
# ---------------------------------------------------------------------------


async def test_assign_existing_tasks_commits_once(tmp_path) -> None:
    """Mode 3 assigns N task_ids; we expect exactly one COMMIT.

    The refactor groups the UPDATE statements into a single
    `executemany` (item 5). Pre-refactor: one UPDATE per task + one
    UPDATE for agents.current_task + one final commit (which is
    still one commit since sqlite3 default isolation auto-batches).
    Post-refactor: one `executemany` for tasks + one UPDATE for the
    agent + one commit. We assert the commit-count invariant: never
    more than one COMMIT per call regardless of task_ids length.
    """
    from agent_mcp.db import connection as conn_mod

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("worker-bulk")

        # Seed 5 unassigned tasks via /api/tasks (the same endpoint
        # the dashboard uses).
        task_ids = []
        for i in range(5):
            r = admin.post(
                "/api/tasks",
                json={
                    "task_title": f"bulk-task-{i}",
                    "task_description": "x",
                },
            )
            assert r.status_code == 200
            task_ids.append(r.json()["task_id"])

        # Spy on Connection.commit so we can count the calls during
        # the assign tool invocation. Post-D1 the Mode-3 assign path
        # acquires its connection through the unit-of-work seam
        # (``unit_of_work()`` -> ``agent_mcp.db.unit_of_work``'s locally
        # bound ``get_db_connection``); pre-D1 it did
        # ``from ..db.connection import get_db_connection`` at the tool
        # module. Both modules bind the name locally, so we patch BOTH
        # module refs to catch the connection regardless of which path
        # opens it (the commit-once invariant is what we pin, not the
        # acquisition site).
        from agent_mcp.db import unit_of_work as uow_mod
        from agent_mcp.tools import task_tools as task_tools_mod

        commit_count = {"n": 0}
        real_get_conn = conn_mod.get_db_connection

        class _CountingConn:
            """Proxy around sqlite3.Connection that increments
            `commit_count` on each `.commit()` call. We use a proxy
            because `sqlite3.Connection.commit` is a read-only
            attribute on the actual instance.
            """

            def __init__(self, inner):
                self._inner = inner

            def commit(self):
                commit_count["n"] += 1
                return self._inner.commit()

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __enter__(self):
                return self._inner.__enter__()

            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        def spy_get_conn(*a, **kw):
            return _CountingConn(real_get_conn(*a, **kw))

        with mock.patch.object(
            task_tools_mod, "get_db_connection", side_effect=spy_get_conn
        ), mock.patch.object(
            uow_mod, "get_db_connection", side_effect=spy_get_conn
        ):
            from agent_mcp.core.tool_result import Ok
            from agent_mcp.tools.task_tools import assign_task_tool_impl

            commit_count["n"] = 0  # reset after seeding's own commits
            # Wave 6 PR 4: assign_task_tool_impl returns ToolResult
            # (Ok/Conflict/Failed/...) rather than list[TextContent].
            # Success here is the Ok variant carrying the human-readable
            # message; pre-migration this was a TextContent block.
            with with_bearer(admin.admin_token):
                result = await assign_task_tool_impl(
                    {
                        "token": admin.admin_token,
                        "agent_id": "worker-bulk",
                        "task_ids": task_ids,
                    }
                )
            assert isinstance(result, Ok), (
                f"assign_task failed: {result!r}"
            )
            assert "✅" in (result.message or "") or "Tasks Assigned" in (
                result.message or ""
            ), f"assign_task succeeded but message unexpected: {result.message!r}"

        # One commit for the whole bulk assign; today's pre-refactor
        # baseline also commits once (sqlite3 default isolation), so
        # this pins the existing invariant against any future
        # accidental per-task commit.
        assert commit_count["n"] == 1, (
            f"expected exactly 1 commit for bulk assign of {len(task_ids)} "
            f"tasks; got {commit_count['n']}"
        )


# ---------------------------------------------------------------------------
# Item 6 — slow-query logging
# ---------------------------------------------------------------------------


_SLOW_QUERY_LOGGER_NAME = "agent_mcp.db.slow_query"
_SLOW_QUERY_THRESHOLD_MS = 100


async def test_slow_query_logger_warns_above_threshold(tmp_path) -> None:
    """A query whose duration crosses the threshold logs a WARNING.

    We monkeypatch the listener's clock so a trivial query appears
    to take 200 ms. The listener must emit a single WARNING through
    the `agent_mcp.db.slow_query` logger with the SQL truncated to
    200 chars and the duration in milliseconds.
    """
    from agent_mcp.db import slow_query as _sq  # the new module
    from agent_mcp.db.engine import get_engine

    async with mcp_session(tmp_path):
        # Drive a trivial ORM query and force the slow-query path.
        engine = get_engine()
        with mock.patch.object(_sq.time, "perf_counter") as fake_clock:
            # First call (before_cursor_execute) returns t0;
            # second call (after_cursor_execute) returns t0 + 0.2s.
            fake_clock.side_effect = [0.0, 0.2]
            with (
                self_capturing_logs(_SLOW_QUERY_LOGGER_NAME) as records,
                engine.connect() as conn,
            ):
                conn.exec_driver_sql("SELECT 1")

        warnings = [r for r in records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, (
            f"expected exactly 1 slow-query WARNING, got "
            f"{[(r.levelno, r.getMessage()) for r in records]}"
        )
        msg = warnings[0].getMessage()
        assert "SELECT 1" in msg
        assert "200" in msg or "200.0" in msg, (
            f"duration not surfaced in log: {msg!r}"
        )


async def test_slow_query_logger_silent_below_threshold(tmp_path) -> None:
    """Queries faster than the threshold leave no warning behind."""
    from agent_mcp.db.engine import get_engine

    async with mcp_session(tmp_path):
        engine = get_engine()
        with (
            self_capturing_logs(_SLOW_QUERY_LOGGER_NAME) as records,
            engine.connect() as conn,
        ):
            conn.exec_driver_sql("SELECT 1")

        warnings = [r for r in records if r.levelno >= logging.WARNING]
        assert not warnings, (
            f"unexpected slow-query WARNING for a trivial SELECT: "
            f"{[r.getMessage() for r in warnings]}"
        )


async def test_slow_query_logger_truncates_sql(tmp_path) -> None:
    """SQL longer than 200 chars is truncated with a `…` indicator."""
    from agent_mcp.db import slow_query as _sq
    from agent_mcp.db.engine import get_engine

    long_sql = "SELECT '" + ("x" * 500) + "'"
    async with mcp_session(tmp_path):
        engine = get_engine()
        with mock.patch.object(_sq.time, "perf_counter") as fake_clock:
            fake_clock.side_effect = [0.0, 0.5]
            with (
                self_capturing_logs(_SLOW_QUERY_LOGGER_NAME) as records,
                engine.connect() as conn,
            ):
                conn.exec_driver_sql(long_sql)

        warnings = [r for r in records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        # 200-char truncation + ellipsis marker.
        # We don't pin the exact ellipsis character (… vs ...) to keep
        # the implementation room.
        assert len(msg) < 400, (
            f"slow-query log not truncated; len={len(msg)}: {msg!r}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def self_capturing_logs(logger_name: str):
    """Capture log records for `logger_name` (and its children) into
    a list; restore handlers on exit.

    Avoids pytest's caplog because that fixture interacts poorly with
    the harness's worker-thread lifespan startup.
    """
    logger = logging.getLogger(logger_name)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
