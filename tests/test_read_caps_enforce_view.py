"""Read-cap enforcement: ``view_project_context`` requires ``memories.view``
and ``get_agent_messages`` requires ``messages.view``.

Closes the one real over-admit found in the round-2 architecture review's
capability audit: the *identity-only* gates on these two MCP read tools
admitted an ``agent_bearer`` whose ``agent_role`` is ``None`` — a malformed
token whose capability bundle is EMPTY — so it could read project context
and its own messages holding ZERO capabilities. Enforcing the corresponding
``*.view`` cap is a **no-op for every legitimate role** (viewer + operator
project-role bundles and worker + manager agent-role bundles all carry the
read caps; sysadmin holds the wildcard) and denies only that empty-bundle
bearer — the exact class the ``rag.query`` gate on ``ask_project_rag``
already closed.

Deliberately NOT enforced (recorded so a future reviewer doesn't re-open it):
  * ``system.view`` — it is granted in the VIEWER bundle, i.e. *more*
    permissive than the ``system.config.write`` gate that ``view_status`` /
    system reads deliberately use. Wiring it would WIDEN viewer access to
    every agent's status + working directory (a regression), so it stays
    unenforced. (Signposted at ``tools/admin_tools.py`` ``view_status``.)
  * ``agents.view`` — a pure no-op formalization: every principal that
    reaches the REST agent-read routes already holds it, and agent bearers
    don't reach those routes. Left alone to keep this change scoped to the
    two gates that close a real hole.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.authorize import AuthRejected
from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import PermissionDenied
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _empty_cap_bearer(agent_id: str = "ghost", token: str = "ghost-tok") -> Principal:
    """An ``agent_bearer`` that identifies an agent but carries NO caps
    (``agent_role=None`` → empty bundle) — the malformed-token over-admit."""
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=None,
        can_wake_loop=False,
        source_token=token,
    )


def _worker_bearer(agent_id: str, token: str) -> Principal:
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token=token,
    )


async def test_empty_cap_bearer_carries_no_read_caps() -> None:
    """Sanity: the malformed bearer really holds neither read cap (so the
    denials below are about the caps, not some other gate)."""
    p = _empty_cap_bearer()
    assert not p.has_capability("memories.view")
    assert not p.has_capability("messages.view")
    # ...while a worker legitimately holds both (the no-op case).
    w = _worker_bearer("alice", "tok")
    assert w.has_capability("memories.view")
    assert w.has_capability("messages.view")


async def test_view_project_context_requires_memories_view(tmp_path) -> None:
    from agent_mcp.tools.project_context_tools import (
        view_project_context_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        # RED before the fix: an empty-cap bearer was ADMITTED to context
        # reads. Phase 2 (Finding A): the gate is now
        # ``@requires_capability("memories.view")`` on the impl, so the
        # denial raises ``AuthRejected`` instead of returning
        # ``PermissionDenied`` — same cap, same decision, same 403.
        with pytest.raises(AuthRejected) as excinfo:
            await view_project_context_tool_impl(
                {}, principal=_empty_cap_bearer()
            )
        assert "memories.view" in excinfo.value.reason

        # No-op for a real worker (its bundle carries memories.view).
        alice = await admin.create_worker("alice")
        ok = await view_project_context_tool_impl(
            {}, principal=_worker_bearer("alice", alice.token)
        )
        assert not isinstance(ok, PermissionDenied), (
            f"worker holds memories.view; must pass the cap gate, got {ok!r}"
        )


async def test_get_agent_messages_requires_messages_view(tmp_path) -> None:
    from agent_mcp.tools.agent_communication_tools import (
        get_agent_messages_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        # Phase 2 (Finding A): the identity + cap pair is now the
        # ``@requires_predicate`` on the impl, so the denial raises
        # ``AuthRejected`` — same two clauses, same decision, same 403.
        with pytest.raises(AuthRejected) as excinfo:
            await get_agent_messages_tool_impl(
                {}, principal=_empty_cap_bearer()
            )
        assert "messages.view" in excinfo.value.reason

        alice = await admin.create_worker("alice")
        ok = await get_agent_messages_tool_impl(
            {}, principal=_worker_bearer("alice", alice.token)
        )
        assert not isinstance(ok, PermissionDenied), (
            f"worker holds messages.view; must pass the cap gate, got {ok!r}"
        )
