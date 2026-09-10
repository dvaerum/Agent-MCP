"""Test suite for PR-4 of the database review improvements.

Covers item 6 from the 2026-06-02 review:

  * Item 6 — SQLAlchemy event-listener for slow queries (threshold
    100 ms). We monkeypatch the listener's clock so a trivial query
    appears to cross the threshold and confirm the logger surfaces a
    warning containing the truncated SQL.

Item 5 (bulk writes commit once, not per-row) used to be pinned here
too, via `assign_task_tool_impl` -- that tool is superseded by the
Rust port (`conexus-tools::task_tools::AssignTaskTool`) and its
supporting `agent_mcp.db.unit_of_work` module is itself a Phase F
deletion candidate, so that test is gone along with them (Phase F,
prancy-napping-pie). The commit-once invariant it pinned is preserved
on the Rust side: `AssignTaskTool` wraps its whole bulk-assign body in
one `rusqlite` transaction (see conexus-tools/src/assign_task_tools.rs).

Phase F: rewritten to boot the DB layer directly instead of the
now-retired tests.harness.mcp_session/full app stack -- slow-query
logging is pure SQLAlchemy-engine/schema coverage with no dependency
on the Python tool/app layer.
"""

from __future__ import annotations

import contextlib
import logging
import os
from unittest import mock


def _bootstrap_fresh_db(tmp_path) -> None:
    """Point the ORM engine at a fresh per-tmpdir DB and run
    init_database() -- the same production bootstrap sequence
    tests/test_migration_*.py already use, with no app/mcp_session
    boot needed."""
    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    os.environ["MCP_PROJECT_DIR"] = project_dir

    from agent_mcp.db import engine as _engine
    _engine._engine = None  # type: ignore[attr-defined]

    from agent_mcp.db.schema import init_database
    init_database()


_SLOW_QUERY_LOGGER_NAME = "agent_mcp.db.slow_query"
_SLOW_QUERY_THRESHOLD_MS = 100


def test_slow_query_logger_warns_above_threshold(tmp_path) -> None:
    """A query whose duration crosses the threshold logs a WARNING.

    We monkeypatch the listener's clock so a trivial query appears
    to take 200 ms. The listener must emit a single WARNING through
    the `agent_mcp.db.slow_query` logger with the SQL truncated to
    200 chars and the duration in milliseconds.
    """
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db import slow_query as _sq
    from agent_mcp.db.engine import get_engine

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


def test_slow_query_logger_silent_below_threshold(tmp_path) -> None:
    """Queries faster than the threshold leave no warning behind."""
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db.engine import get_engine

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


def test_slow_query_logger_truncates_sql(tmp_path) -> None:
    """SQL longer than 200 chars is truncated with a `…` indicator."""
    _bootstrap_fresh_db(tmp_path)
    from agent_mcp.db import slow_query as _sq
    from agent_mcp.db.engine import get_engine

    long_sql = "SELECT '" + ("x" * 500) + "'"
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
