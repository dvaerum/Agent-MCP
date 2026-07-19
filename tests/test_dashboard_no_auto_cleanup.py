"""Regression: the dashboard must not auto-terminate worker agents.

Background
----------
``agents-dashboard.tsx`` previously installed a ``setInterval`` that
fired every 2 minutes while any browser tab had the agents view open.
It called ``getIdleAgentsForCleanup()`` (from
``lib/stores/data-store.ts``) which returned every agent matching:

* not Admin/admin
* no ``current_task``
* ``status != 'terminated'``
* created > 10 minutes ago

For each matching agent it called ``apiClient.terminateAgent(agentId)``,
silently killing valid worker agents (e.g. ``backend-dev``,
``ios-app-dev``) every 2 minutes for as long as the tab stayed open.

Confirmed in production audit log: workers got restored, then
re-terminated 7 minutes later by ``admin`` (the dashboard's bearer).

A companion bug in ``shouldDisplayAgent`` hid the same agents from the
table once they were >10 minutes old without a task — so they vanished
from the UI *and* got auto-killed.

Fix (this PR)
-------------
1. Delete the auto-cleanup ``useEffect`` in ``agents-dashboard.tsx``.
2. Drop ``getIdleAgentsForCleanup`` from ``data-store.ts``.
3. Drop the ``idleForCleanup`` stat + "N pending cleanup" badge.
4. Fix ``shouldDisplayAgent`` to surface all non-terminated agents
   regardless of age.

Auto-terminating is the wrong default. Agents should only be
terminated by explicit user action.

These tests are split into two tiers:

* **Static asserts** parse the .tsx/.ts as text (matching the
  convention in `test_dashboard_agent_restore_purge.py` since the
  dashboard has no JS test runner) and guard the source-level
  invariants — the cleanup ``useEffect`` is gone, the cleanup
  selector is gone, the badge is gone, ``shouldDisplayAgent`` no
  longer hides aged agents.
* **Server-side asserts** use ``mcp_session`` to drive
  ``/api/all-data`` with a seeded "old idle" agent and prove the
  server has no auto-terminate behavior of its own — the row is
  returned, untouched, with ``status != 'terminated'``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.harness import mcp_session


DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------------------------------------------------------------------------
# Static (source-level) asserts — these run without any harness.
# ---------------------------------------------------------------------------


def test_agents_dashboard_has_no_auto_cleanup_interval() -> None:
    """The ``setInterval`` that fired every 2 minutes and called
    ``handleTerminateAgent`` on every "idle" worker must be gone."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    # The most specific signal: the cleanup loop logged this banner.
    assert "Found" not in src or "idle agents for cleanup" not in src, (
        "agents-dashboard.tsx still contains the auto-cleanup loop "
        "(`Found N idle agents for cleanup` log message). The "
        "auto-terminate useEffect must be deleted entirely."
    )
    # Broader signal: no reference to getIdleAgentsForCleanup at all.
    assert "getIdleAgentsForCleanup" not in src, (
        "agents-dashboard.tsx still references getIdleAgentsForCleanup. "
        "The auto-cleanup selector and every caller must be removed."
    )


def test_data_store_drops_idle_cleanup_selector() -> None:
    """``getIdleAgentsForCleanup`` should be gone from data-store.ts —
    no callers post-fix, and keeping it invites the same bug to
    sneak back in."""
    src = _read("lib/stores/data-store.ts")
    assert "getIdleAgentsForCleanup" not in src, (
        "lib/stores/data-store.ts still defines getIdleAgentsForCleanup. "
        "Delete the selector; with the cleanup useEffect gone it has "
        "no callers."
    )


def test_agents_dashboard_drops_pending_cleanup_badge() -> None:
    """The orange "N pending cleanup" badge tracked the size of the
    cleanup queue. With auto-cleanup gone the badge is meaningless and
    misleading — it must be removed."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    assert "pending cleanup" not in src, (
        "agents-dashboard.tsx still renders the 'N pending cleanup' "
        "badge. Remove the badge and the stats.idleForCleanup field."
    )
    assert "idleForCleanup" not in src, (
        "agents-dashboard.tsx still references stats.idleForCleanup. "
        "Drop the stat — with no auto-cleanup it has no meaning."
    )


def test_should_display_agent_does_not_hide_aged_agents() -> None:
    """``shouldDisplayAgent`` previously returned ``false`` for any
    non-admin agent older than 10 minutes without a current_task,
    hiding them from the Agents table entirely. The fix is to surface
    every non-terminated agent regardless of age — the existing status
    filter in the UI already lets users hide terminated rows if they
    want."""
    src = _read("lib/stores/data-store.ts")
    # The age-based filter relied on these two patterns. If either is
    # still present in shouldDisplayAgent, the regression is back.
    #
    # We look for the literal "10" age constant *inside* the
    # shouldDisplayAgent body. A cheap heuristic: check that the
    # function body no longer mentions ageInMinutes.
    assert "ageInMinutes" not in src or "shouldDisplayAgent" not in src, (
        "data-store.ts still ties shouldDisplayAgent to ageInMinutes. "
        "Remove the age check — show all non-terminated agents."
    )


# ---------------------------------------------------------------------------
# Server-side asserts — prove the bug was UI-only.
# ---------------------------------------------------------------------------


pytestmark_async = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_all_data_returns_old_idle_agent(tmp_path) -> None:
    """Seed a worker agent whose ``created_at`` is two years old and
    which has no ``current_task``. ``/api/all-data`` must include it
    in the returned ``agents`` list (the server doesn't run any
    age-based hiding) — proves the bug was purely client-side and
    that the fix needs no server change to surface old workers."""
    async with mcp_session(tmp_path) as admin:
        # Insert a worker directly so we can set created_at in the past.
        from agent_mcp.db.connection import get_db_connection

        old_ts = "2024-01-01T00:00:00"
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO agents (token, agent_id, "
                "created_at, status, working_directory, color, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "__test_old_worker_token",
                    "old-worker",
                    old_ts,
                    "created",
                    "/tmp",
                    "#888",
                    old_ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        resp = admin.get("/api/all-data")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        agents = body.get("agents", [])
        old = [a for a in agents if a.get("agent_id") == "old-worker"]
        assert len(old) == 1, (
            f"Expected old-worker in /api/all-data agents list, got: "
            f"{[a.get('agent_id') for a in agents]}"
        )
        # And it must NOT be terminated — the server has no
        # auto-cleanup of its own.
        assert old[0]["status"] != "terminated", (
            f"Server auto-terminated an idle worker; status="
            f"{old[0]['status']!r}. The agent-deletion model must "
            f"remain 'explicit user action only'."
        )


@pytest.mark.asyncio
async def test_no_server_side_idle_cleanup_endpoint(tmp_path) -> None:
    """Guard against a 'soft replacement' regression where someone
    re-implements the auto-cleanup on the server (a sync endpoint, a
    background task, anything). The agent-deletion model is 'explicit
    user action only' — no auto-terminate path may exist.

    We can't enumerate every possible code path, but we can pin two
    properties:

    1. There is no ``/api/auto-cleanup`` / ``/api/cleanup-idle`` /
       ``/api/terminate-idle`` route registered.
    2. An idle worker that's been sitting for several "ticks" of the
       app stays in status='created' (no background task flips it).
    """
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.db.connection import get_db_connection

        old_ts = "2024-01-01T00:00:00"
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO agents (token, agent_id, "
                "created_at, status, working_directory, color, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "__test_idle_worker_token",
                    "idle-worker",
                    old_ts,
                    "created",
                    "/tmp",
                    "#888",
                    old_ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Check obvious auto-cleanup endpoints don't exist (no 2xx).
        # The catch-all `/api/{path:path}` OPTIONS handler turns
        # unknown paths into 405 on GET, which is also "not handled."
        for path in (
            "/api/auto-cleanup",
            "/api/cleanup-idle",
            "/api/terminate-idle",
        ):
            r = admin.client.get(path)
            assert r.status_code >= 400, (
                f"Auto-cleanup endpoint {path!r} returned "
                f"{r.status_code}; the server must NOT expose an "
                f"auto-terminate surface."
            )

        # And after a /api/all-data round trip the row's status is
        # unchanged — no implicit side-effect cleanup.
        admin.get("/api/all-data")
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT status FROM agents WHERE agent_id = ?",
                ("idle-worker",),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, "idle-worker row vanished after /api/all-data"
        assert row[0] != "terminated", (
            f"idle-worker was auto-terminated after /api/all-data "
            f"(status={row[0]!r}). No code path may auto-terminate "
            f"agents."
        )
