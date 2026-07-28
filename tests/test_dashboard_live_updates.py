"""Live dashboard updates: every mutation pushes a `resources/updated`
notification to dashboard SSE subscribers, so open Tasks/Messages/
Agents/Memories pages refetch without a manual refresh.

The single choke point is `log_agent_action_to_db` — every mutating
tool logs an action there (it's the Recent-activity source), so hooking
the dashboard fan-out there covers all current AND future mutations with
no per-site sprinkling. The fan-out targets runtime-queue subscribers
(the dashboard's SSE stream); agents parked in wait_for_events POSTs have
no runtime queue, so they aren't spammed.
"""

from __future__ import annotations

import sqlite3

from agent_mcp.core import session_registry
from agent_mcp.db.actions import agent_actions_db


def _mk_cursor() -> sqlite3.Cursor:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE agent_actions (agent_id TEXT, action_type TEXT, "
        "task_id TEXT, timestamp TEXT, details TEXT)"
    )
    return conn.cursor()


def test_action_log_pushes_scoped_resources_updated(monkeypatch) -> None:
    """Logging a task mutation fans out a `notifications/resources/updated`
    with a task-scoped URI to all runtime-queue (dashboard) subscribers."""
    calls = []
    monkeypatch.setattr(
        session_registry, "fanout_to_all",
        lambda payload: calls.append(payload) or [],
    )
    cur = _mk_cursor()
    agent_actions_db.log_agent_action_to_db(
        cur, agent_id="worker", action_type="assign_task", task_id="t1",
    )
    assert calls, "expected a dashboard fan-out on the mutation"
    p = calls[0]
    assert p["method"] == "notifications/resources/updated"
    assert p["params"]["uri"].startswith("agent-mcp://")
    assert "tasks" in p["params"]["uri"], p


def test_action_log_scopes_by_action_type(monkeypatch) -> None:
    """The URI scope is derived from the action_type so a future
    fine-grained dashboard can invalidate just the touched slice."""
    calls = []
    monkeypatch.setattr(
        session_registry, "fanout_to_all",
        lambda payload: calls.append(payload) or [],
    )
    cases = {
        "send_message": "messages",
        "create_agent": "agents",
        "update_project_context": "memories",
    }
    for action, expected_scope in cases.items():
        calls.clear()
        agent_actions_db.log_agent_action_to_db(
            _mk_cursor(), agent_id="a", action_type=action,
        )
        assert calls, action
        assert expected_scope in calls[0]["params"]["uri"], (action, calls[0])


def test_action_log_never_raises_when_fanout_breaks(monkeypatch) -> None:
    """The push is best-effort telemetry — a broken fan-out must never
    disrupt the mutation that logged the action."""
    def boom(_payload):
        raise RuntimeError("session registry exploded")

    monkeypatch.setattr(session_registry, "fanout_to_all", boom)
    cur = _mk_cursor()
    # Must not raise.
    agent_actions_db.log_agent_action_to_db(
        cur, agent_id="worker", action_type="create_task", task_id="t2",
    )
    # And the action row was still written.
    row = cur.execute(
        "SELECT action_type FROM agent_actions"
    ).fetchone()
    assert row and row[0] == "create_task"
