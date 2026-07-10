"""BL-R31-3b: exclude tombstone rows from the remaining raw-SQL /
cache-warm siblings that PR #379's class-sweep left out of scope.

PR #379 (BL-R31-3) closed the tombstone leak at the ORM repository
layer (``get_all_active_agents_from_db`` + ``AgentRepository.query``)
so MCP ``view_agents`` matches the REST agent-list surfaces. Its
class-sweep flagged same-class siblings that do NOT route through the
repository and therefore still surface / cache tombstone rows:

  1. ``features/dashboard/api.py::fetch_graph_data_logic`` — raw
     ``WHERE status != 'terminated'`` renders a tombstone as a
     visible ``[deleted-<id>]`` GRAPH NODE (the real leak surface).
  2. ``core/state.py::notify_unassigned_task_appeared`` — raw
     ``WHERE status != 'terminated'`` fans an unassigned-task event
     out to tombstone rows.
  3. ``app/server_lifecycle.py`` startup load — raw
     ``WHERE status != 'terminated'`` loads a persisted tombstone
     row into the ``g.active_agents`` auth cache on the NEXT restart
     after a purge.
  4. ``repositories/agent_repository.py`` cache-warm gates
     (``get_by_id`` / ``get_by_token``) — the ``!= 'terminated'``
     write gate lets a tombstone row poison ``state.active_agents``
     on a by-id/by-token lookup, which then leaks via the operator
     token listing.

"Active" agents must exclude BOTH ``'terminated'`` AND ``'tombstone'``
everywhere. These tests fail on the pre-fix code.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.harness import mcp_session


def _insert_tombstone(agent_id: str) -> None:
    """Mirror the purge cascade's INSERT OR IGNORE of the tombstone row
    (verbatim from ``insert_tombstone`` / test_purge_drops_visible_count).
    """
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO agents "
            "(token, agent_id, capabilities, created_at, status, "
            " working_directory, color, updated_at) "
            "VALUES (?, ?, '[]', ?, 'tombstone', '', '#000000', ?)",
            (
                f"__tombstone_{agent_id}",
                f"[deleted-{agent_id}]",
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------- 1. dashboard graph (the real leak) -------------------


@pytest.mark.asyncio
async def test_graph_data_excludes_tombstone_nodes(tmp_path) -> None:
    """GET /api/graph-data (``fetch_graph_data_logic``) must NOT render
    a tombstone row as a ``[deleted-<id>]`` agent node. A live agent
    must still appear (regression)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _insert_tombstone("ghost")

        resp = admin.get("/api/graph-data")
        assert resp.status_code == 200, resp.text
        nodes = resp.json().get("nodes", [])
        node_ids = [n.get("id") for n in nodes]
        labels = [n.get("label") for n in nodes]

        # Regression: the live agent is still a graph node.
        assert "agent_alice" in node_ids, node_ids

        # Leak: no tombstone node.
        assert "agent_[deleted-ghost]" not in node_ids, (
            f"tombstone rendered as a graph node: {node_ids}"
        )
        assert not any(
            isinstance(lbl, str) and lbl.startswith("[deleted-") for lbl in labels
        ), f"tombstone [deleted-*] label leaked into graph: {labels}"


# ---------------- 2. notify_unassigned_task_appeared -------------------


@pytest.mark.asyncio
async def test_notify_unassigned_task_skips_tombstone(tmp_path) -> None:
    """``notify_unassigned_task_appeared`` must not fan an event out to
    a tombstone row. A live agent still gets notified (regression)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _insert_tombstone("ghost")

        # The fan-out returns early unless the source task row exists.
        from agent_mcp.db.connection import get_db_connection

        now = _dt.datetime.now().isoformat()
        conn = get_db_connection()
        try:
            conn.cursor().execute(
                "INSERT INTO tasks (task_id, title, description, status, "
                "priority, assigned_to, created_by, created_at, updated_at, "
                "parent_task) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("task-tomb-fanout", "t", "d", "pending", "medium", None,
                 "admin", now, now, None),
            )
            conn.commit()
        finally:
            conn.close()

        from agent_mcp.core import state as _state
        from agent_mcp.core import event_bus as _event_bus

        notified: list[str] = []
        orig_notify = _event_bus.notify

        def _spy(agent_id, event_type, payload):  # noqa: ANN001
            notified.append(agent_id)
            return None

        _event_bus.notify = _spy
        try:
            # Empty required-caps => wake every active agent (subset of
            # any capability set), so the tombstone would be included
            # pre-fix.
            _state.notify_unassigned_task_appeared("task-tomb-fanout", [])
        finally:
            _event_bus.notify = orig_notify

        assert "alice" in notified, (
            f"live agent should be notified for an unassigned task: {notified}"
        )
        assert "[deleted-ghost]" not in notified, (
            f"tombstone row was notified of an unassigned task: {notified}"
        )


# ---------------- 3. server_lifecycle startup load ---------------------


@pytest.mark.asyncio
async def test_startup_load_excludes_tombstone_from_active_cache(tmp_path) -> None:
    """A tombstone row persisted in the DB must NOT be loaded into the
    ``g.active_agents`` auth cache on the next startup. Simulated as
    two sequential lifespans over the same project DB: session 1 writes
    the tombstone, session 2's startup load reads the same DB."""
    # Session 1: persist a tombstone row into the project DB.
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _insert_tombstone("ghost")

    # Session 2: fresh lifespan re-loads state from the same DB file.
    async with mcp_session(tmp_path):
        from agent_mcp.core import globals as g

        loaded = list(g.active_agents.values())
        agent_ids = [v.get("agent_id") for v in loaded]
        statuses = [v.get("status") for v in loaded]
        tokens = list(g.active_agents.keys())

        # Regression: the live agent survives the reload.
        assert "alice" in agent_ids, agent_ids

        assert "[deleted-ghost]" not in agent_ids, (
            f"tombstone loaded into g.active_agents on startup: {agent_ids}"
        )
        assert "tombstone" not in statuses, (
            f"a status='tombstone' row is in the active-agents cache: "
            f"{list(zip(agent_ids, statuses))}"
        )
        assert "__tombstone_ghost" not in tokens, (
            f"reserved tombstone token leaked into the auth allow-list: {tokens}"
        )


# ---------------- 4. cache-warm gate (get_by_id lookup) ----------------


@pytest.mark.asyncio
async def test_get_by_id_does_not_warm_cache_with_tombstone(tmp_path) -> None:
    """A by-id lookup that resolves to a tombstone row must NOT warm the
    ``state.active_agents`` cache with it (otherwise the operator token
    listing, which iterates the cache filtering ``!= 'terminated'``,
    leaks the tombstone). The row is still RETURNED for audit."""
    async with mcp_session(tmp_path):
        _insert_tombstone("ghost")

        from agent_mcp.core import state as _state
        from agent_mcp.repositories import agent_repo

        # Ensure a clean cache slate for the reserved token.
        _state.active_agents.pop("__tombstone_ghost", None)

        row = agent_repo.get_by_id("[deleted-ghost]")

        # The row resolves (audit paths still work)...
        assert row is not None and row.get("status") == "tombstone", row
        # ...but it MUST NOT have been written into the auth cache.
        assert "__tombstone_ghost" not in _state.active_agents, (
            "tombstone row poisoned the active-agents cache via get_by_id"
        )
        assert not any(
            v.get("status") == "tombstone" for v in _state.active_agents.values()
        ), "a tombstone row is present in state.active_agents"


# ---------------- 5. assignment target gate ----------------------------


@pytest.mark.asyncio
async def test_tombstone_is_not_an_assignable_target(tmp_path) -> None:
    """``_agent_assignable`` decides whether a task may be pinned onto an
    agent. A tombstone row (`[deleted-<id>]`, status='tombstone') is a
    purge FK artefact, NOT a live agent — pinning work onto it is
    unreachable work attributed to a deleted identity. A real live agent
    must still be assignable (regression)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _insert_tombstone("ghost")

        from agent_mcp.tools.task_tools import _agent_assignable
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            assert _agent_assignable(cursor, "alice") is True, (
                "a live agent must remain an assignable target"
            )
            assert _agent_assignable(cursor, "[deleted-ghost]") is False, (
                "a tombstone row must not be an assignable task target"
            )
        finally:
            conn.close()


# ---------------- 6. terminate target gate -----------------------------


@pytest.mark.asyncio
async def test_tombstone_is_not_a_terminate_target(tmp_path) -> None:
    """``AgentRepository.terminate`` must refuse a tombstone row. A
    tombstone is already a purge artefact; flipping its status to
    'terminated' would leak `[deleted-<id>]` into the
    terminated-agents listing (GET /api/agents?status=terminated). A
    live agent must still be terminatable (regression)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _insert_tombstone("ghost")

        from agent_mcp.repositories import agent_repo
        from agent_mcp.db.connection import get_db_connection

        # A tombstone is not a terminate target.
        assert agent_repo.terminate("[deleted-ghost]") is False, (
            "terminate() must refuse a tombstone row"
        )

        # And its status is untouched (still 'tombstone', not flipped).
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT status FROM agents WHERE agent_id = ?", ("[deleted-ghost]",)
            )
            assert cur.fetchone()["status"] == "tombstone", (
                "terminate() mutated a tombstone row's status"
            )
        finally:
            conn.close()

        # Regression: a live agent is still terminatable.
        assert agent_repo.terminate("alice") is True, (
            "a live agent must remain terminatable"
        )
