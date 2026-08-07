"""Regression: post-restart hydration must carry ``agent_role`` so a
previously-registered agent's bearer keeps its role capabilities.

Bug (pentest R5-F2, fail-closed availability)
---------------------------------------------
On backend/router restart, ``application_startup`` rehydrates the
``g.active_agents`` auth cache from the DB. The old hand-rolled SELECT
omitted ``agent_role`` and wrote a **roleless** dict into the cache.

The consequence is fail-closed but total:

1. Hydration writes a roleless row into ``active_agents[token]``.
2. The bearer hits ``/mcp`` → ``_bearer_is_active(token)`` (cache-only)
   → token present → authenticates.
3. ``build_agent_bearer_principal`` → ``get_by_token`` → cache HIT →
   the DB fallback (``_agent_to_dict``, which WOULD carry ``agent_role``)
   never fires.
4. ``normalize_agent_role(None)`` → ``None`` →
   ``resolve_capabilities(kind="agent_bearer", agent_role=None)`` →
   ``frozenset()`` → EVERY role verb (rag.query, tasks.create, …) denied
   until the agent re-registers.

This is the missed sibling of a class already fixed for the *restore*
cache-write (``admin_tools`` — "Omitting it made a restored manager
transiently resolve to worker capabilities"). The fix routes hydration
through the repository's canonical row-builder
(``get_all_active_agents_from_db`` → ``_agent_to_dict``) so the cache-row
shape — including ``agent_role`` — has ONE source of truth shared with
the auth hot path and the register/restore cache writes.
"""

from __future__ import annotations

import secrets

import pytest

from agent_mcp.core import globals as g
from agent_mcp.core.capabilities import AGENT_ROLE_BUNDLES
from agent_mcp.core.principal_builder import build_agent_bearer_principal
from tests.conftest import seed_agent_row
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_active_agent(agent_id: str, agent_role: str) -> str:
    """Insert an ACTIVE agent row with the given role. Returns its token."""
    from agent_mcp.db.connection import get_db_connection

    token = f"tok_{secrets.token_hex(8)}"
    conn = get_db_connection()
    try:
        seed_agent_row(
            conn,
            agent_id,
            token=token,
            role=agent_role,
            status="active",
        )
    finally:
        conn.close()
    return token


async def test_hydration_carries_agent_role_so_manager_keeps_caps(
    tmp_path,
) -> None:
    """A manager agent seeded in the DB, then re-hydrated into the cache
    the way ``application_startup`` does after a restart, must resolve to
    the FULL manager capability bundle — not the empty set the roleless
    hydration produced (pentest R5-F2)."""
    from agent_mcp.app.server_lifecycle import _hydrate_active_agents_cache

    async with mcp_session(tmp_path):
        token = _seed_active_agent("mgr-1", "manager")

        # Simulate a fresh restart: clear the cache, then re-hydrate from
        # the DB the same way lifespan startup does.
        g.active_agents.clear()
        g.agent_working_dirs.clear()
        _hydrate_active_agents_cache()

        # The cached row itself must carry the role — the cache HIT in
        # get_by_token suppresses the DB fallback, so the role MUST be
        # correct in-cache or the principal collapses to empty caps.
        cached = g.active_agents.get(token)
        assert cached is not None, "manager row missing from hydrated cache"
        assert cached.get("agent_role") == "manager", (
            f"hydrated cache row is roleless: {cached!r}"
        )

        principal = build_agent_bearer_principal(token)
        assert principal is not None
        assert principal.agent_role == "manager"
        assert principal.has_capability("rag.query") is True
        assert principal.has_capability("tasks.create") is True
        # manager-tier verbs the worker bundle does NOT include.
        assert principal.has_capability("tasks.assign") is True
        assert principal.has_capability("memories.update") is True
        assert len(principal.capabilities) == len(
            AGENT_ROLE_BUNDLES["manager"]
        )


async def test_hydration_worker_resolves_to_worker_bundle_not_empty_or_manager(
    tmp_path,
) -> None:
    """Regression companion: a worker hydrates to the worker bundle —
    not empty (the bug), not the manager bundle (over-grant)."""
    from agent_mcp.app.server_lifecycle import _hydrate_active_agents_cache

    async with mcp_session(tmp_path):
        token = _seed_active_agent("wrk-1", "worker")

        g.active_agents.clear()
        g.agent_working_dirs.clear()
        _hydrate_active_agents_cache()

        cached = g.active_agents.get(token)
        assert cached is not None
        assert cached.get("agent_role") == "worker"

        principal = build_agent_bearer_principal(token)
        assert principal is not None
        assert principal.agent_role == "worker"
        assert principal.has_capability("rag.query") is True
        # Worker must NOT carry the manager-only verbs.
        assert principal.has_capability("tasks.assign") is False
        assert principal.has_capability("memories.update") is False
        assert len(principal.capabilities) == len(
            AGENT_ROLE_BUNDLES["worker"]
        )
