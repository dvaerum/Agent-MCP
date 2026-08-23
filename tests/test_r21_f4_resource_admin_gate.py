"""R21-F4: `resolve_agent_id_for_uri`'s admin cross-agent branch was dead
code for every real operator-tier caller.

``resolve_agent_id_for_uri`` (agent_mcp/resources/__init__.py) is the
authz gate for `resources/read` on `agent-mcp://inbox/<agent_id>` and
`agent-mcp://status/<agent_id>`. Its docstring says admin can read any
agent's resource (operational visibility), gated on
``catalog_role(principal) == "admin"``. But the pre-fix code required
``bearer_agent_id`` to be truthy BEFORE that admin check ever ran — and
every real operator-tier Principal reachable in production
(``build_operator_principal`` — cookie-session or the router's signed
forwarding-header path) is built with ``agent_id=None`` (see
``core/principal_builder.py``). So the early "token does not resolve to
an agent" bail-out always fired first for a genuine sysadmin/operator,
and the admin branch below it was unreachable.

This module pins two things:

* A genuine operator-tier admin (agent_id=None, catalog_role=="admin")
  CAN read another agent's inbox/status resource — the documented
  behavior, RED pre-fix / GREEN post-fix.
* A non-admin agent-bearer (own agent_id set, worker/manager role) is
  STILL denied reading another agent's resource — the core cross-agent
  IDOR protection this lane already held, which must not regress.
"""

from __future__ import annotations

import json
from pathlib import Path

import mcp.types as mcp_types
import pytest

# ---------------------------------------------------------------------------
# Admin-success case: a genuine operator-tier Principal (agent_id=None)
# must resolve another agent's URI straight from the URI itself.
# ---------------------------------------------------------------------------


def test_operator_admin_with_no_agent_id_resolves_other_agents_uri() -> None:
    """Unit-level reproduction, no MCP wire / DB needed:
    `resolve_agent_id_for_uri` must consult `catalog_role` BEFORE
    requiring `bearer_agent_id` to be truthy — an operator-tier admin
    Principal (cookie session / router forwarding-header) legitimately
    carries `agent_id=None`.

    RED pre-fix: raises ResourceReadError("token does not resolve to an
    agent") because the truthy-bearer_agent_id bail-out runs first.
    GREEN post-fix: returns "bob" (the URI's target agent), no bearer
    agent_id required at all.
    """
    from agent_mcp.core.principal_builder import build_operator_principal
    from agent_mcp.resources import resolve_agent_id_for_uri

    sysadmin_principal = build_operator_principal(
        user_id="sysadmin-user",
        kind="operator_session",
        project_role=None,
        sysadmin=True,
    )
    assert sysadmin_principal.agent_id is None, (
        "fixture must reproduce the real operator-tier shape: "
        "agent_id=None"
    )

    resolved = resolve_agent_id_for_uri(
        "agent-mcp://status/bob", None, principal=sysadmin_principal
    )
    assert resolved == "bob"


def test_operator_admin_with_no_agent_id_resolves_inbox_uri_too() -> None:
    """Same admin-success shape, inbox URI prefix instead of status —
    both resource prefixes share the same gate."""
    from agent_mcp.core.principal_builder import build_operator_principal
    from agent_mcp.resources import resolve_agent_id_for_uri

    forwarding_admin = build_operator_principal(
        user_id="router-forwarded-admin",
        kind="forwarding_header",
        project_role="operator",
        sysadmin=False,
    )
    assert forwarding_admin.agent_id is None

    resolved = resolve_agent_id_for_uri(
        "agent-mcp://inbox/carol", None, principal=forwarding_admin
    )
    assert resolved == "carol"


@pytest.mark.asyncio
async def test_sysadmin_operator_reads_other_agents_status_over_mcp_wire(
    tmp_path: Path,
) -> None:
    """Full-stack reproduction: a genuine sysadmin operator Principal
    (agent_id=None) issues a real `resources/read` request over the
    registered MCP handler and gets back the target agent's actual
    status payload — not the "token does not resolve to an agent"
    denial.
    """
    from pydantic_core import Url

    from agent_mcp.core.principal_builder import build_operator_principal
    from agent_mcp.tools.registry import request_auth_token, request_principal
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")

        handler = admin._mcp_app_instance().request_handlers[
            mcp_types.ReadResourceRequest
        ]
        req = mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(
                uri=Url("agent-mcp://status/bob")
            ),
        )
        sysadmin_principal = build_operator_principal(
            user_id="sysadmin-user",
            kind="operator_session",
            project_role=None,
            sysadmin=True,
        )
        assert sysadmin_principal.agent_id is None

        cv_principal = request_principal.set(sysadmin_principal)
        cv_token = request_auth_token.set(None)
        try:
            result = await handler(req)
        finally:
            request_principal.reset(cv_principal)
            request_auth_token.reset(cv_token)

        inner = result.root if hasattr(result, "root") else result
        contents = getattr(inner, "contents", None)
        assert contents, "sysadmin operator's cross-agent read failed"
        text = next(
            (c.text for c in contents if getattr(c, "text", None)), ""
        )
        payload = json.loads(text)
        assert payload.get("agent_id") == "bob"


# ---------------------------------------------------------------------------
# Regression guard: a non-admin agent-bearer (own agent_id set) must
# still be denied reading another agent's resource. This is the core
# cross-agent IDOR protection; the reorder above must not weaken it.
# ---------------------------------------------------------------------------


def test_worker_agent_bearer_still_denied_via_direct_resolve() -> None:
    """A worker-role agent-bearer Principal (its own real agent_id,
    catalog_role=="worker") must still be rejected when resolving
    another agent's URI."""
    from agent_mcp.core.principal import Principal
    from agent_mcp.core.principal_builder import catalog_role
    from agent_mcp.resources import ResourceReadError, resolve_agent_id_for_uri

    worker_principal = Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id="alice",
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token="alice-token",
        capabilities=frozenset(),
    )
    assert catalog_role(worker_principal) == "worker"

    with pytest.raises(ResourceReadError):
        resolve_agent_id_for_uri(
            "agent-mcp://status/bob", None, principal=worker_principal
        )


@pytest.mark.asyncio
async def test_worker_agent_bearer_still_denied_over_mcp_wire(
    tmp_path: Path,
) -> None:
    """Full-stack regression guard: a real worker session reading
    another agent's resource over the actual MCP handler is still
    rejected end to end."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")

        from pydantic_core import Url

        from agent_mcp.tools.registry import request_auth_token

        handler = admin._mcp_app_instance().request_handlers[
            mcp_types.ReadResourceRequest
        ]
        req = mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(
                uri=Url("agent-mcp://status/bob")
            ),
        )
        tok = request_auth_token.set(alice.token)
        try:
            with pytest.raises(ValueError):
                await handler(req)
        finally:
            request_auth_token.reset(tok)
