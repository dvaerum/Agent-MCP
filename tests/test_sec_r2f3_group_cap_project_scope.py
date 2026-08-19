"""Pentest R2-F3 — ``group_capability`` grants must not cross projects.

CONFIRMED LIVE (HIGH): :func:`agent_mcp.core.capabilities.resolve_capabilities`
unions a caller's ``group_capability`` rows into their capability set with
NO project dimension — the table is ``(group_id, capability)``, nothing
else. REST is separately gated by ``require_operator_session_middleware``
(blocks non-operator-role mutations regardless of capabilities), but the
``/mcp`` JSON-RPC wire (``POST /agent-mcp/mcp/<project>``, reachable with
just the dashboard session cookie) delegates authorization entirely to each
tool's bare ``has_capability()`` check. A VIEWER-tier member of project A,
who merely belongs to a group an admin granted ``memories.create`` for some
OTHER, unrelated project, could therefore create project-context rows (and,
by the same mechanism, hit every other resource-tier capability gate) in
project A — a cross-project privilege escalation.

Contrast with ``PROJECT_ROLE_BUNDLES`` (sourced from
``project_membership.role``, resolved by ``group_resolver`` including
transitive group ROLE assignments): that path IS correctly project-scoped
— ``resolve_user_project_role_on`` takes ``max(role_rank)`` scoped by
``project_name``. The bug is specific to the SEPARATE
``group_capability`` direct grant table, which has no ``project_name``
column at all.

Fix (root cause, not a workaround): ``resolve_capabilities`` now only
admits ``system.*``-prefixed capabilities sourced from ``group_capability``
— those are legitimately global (router-admin verbs, no project dimension
to violate; see ``Principal.has_capability``'s own ``system.`` short-
circuit). Resource-tier (non-``system.*``) capabilities must flow only
through the already project-scoped ``PROJECT_ROLE_BUNDLES`` path. The
write side (``replace_group_capabilities_handler``) mirrors this by
rejecting an attempt to grant a non-``system.*`` capability to a group
outright (400), rather than silently accepting a grant that becomes a
no-op.

RED on main (pre-fix): the viewer-tier + unrelated-group-``memories.create``
call to ``create_project_context`` succeeds (``Ok``) and the sibling
``delete_task`` decorator gate admits. GREEN (post-fix): both deny.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.authorize import AuthRejected
from agent_mcp.core.capabilities import PROJECT_ROLE_BUNDLES, SYSADMIN_WILDCARD
from agent_mcp.core.tool_result import Ok, PermissionDenied
from tests.harness import make_principal, mcp_session


# ── helpers ──────────────────────────────────────────────────────────


def _patch_group_caps(monkeypatch, group_caps: set[str], *, group_id: str = "g1"):
    """Make the caller resolve to a single group ``group_id`` whose
    ``group_capability`` rows are exactly ``group_caps``.

    Mirrors the ``group_capability`` table shape exactly: a row is just
    ``(group_id, capability)`` — there is nothing here (and nothing in
    the real table) that says which project the grant was "for". That
    absence is the vulnerability; the test deliberately grants the cap
    while the caller is acting on a project that has nothing to do with
    whatever project an admin had in mind when they ticked the box.
    """
    import agent_mcp.repositories.group_capability_repository as gcr
    import agent_mcp.router.group_resolver as gr

    monkeypatch.setattr(gr, "resolve_user_groups", lambda user_id: {group_id})
    monkeypatch.setattr(gcr, "fetch", lambda gid: frozenset(group_caps))


def _viewer_principal_with_group_caps(monkeypatch, group_caps: set[str]):
    """A viewer-tier forwarding-header caller (the ``/mcp`` wire shape)
    whose ONLY path to ``group_caps`` is the group-capability overlay —
    ``PROJECT_ROLE_BUNDLES["viewer"]`` never grants a write cap."""
    _patch_group_caps(monkeypatch, group_caps)
    return make_principal(
        kind="forwarding_header",
        user_id="mallory",
        agent_id=None,
        sysadmin=False,
        project_name="project-a",
        project_role="viewer",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
        groups=None,  # None -> resolve_capabilities self-resolves via the patched resolver
    )


# ── (1) unit-level: resolve_capabilities no longer admits a resource-tier
#        group cap into the returned frozenset ──────────────────────────


def test_resolve_capabilities_drops_resource_tier_group_capability(monkeypatch):
    """A ``memories.create`` row on a group the caller belongs to must NOT
    surface in the resolved capability set for a viewer — resource-tier
    caps have no project dimension in ``group_capability`` and must flow
    only through ``PROJECT_ROLE_BUNDLES``."""
    from agent_mcp.core.capabilities import resolve_capabilities

    _patch_group_caps(monkeypatch, {"memories.create"})

    caps = resolve_capabilities(
        user_id="mallory",
        agent_id=None,
        sysadmin=False,
        agent_role=None,
        project_role="viewer",
        kind="forwarding_header",
    )

    assert "memories.create" not in caps
    # The viewer bundle's own read caps still come through untouched.
    assert "memories.view" in caps


def test_resolve_capabilities_still_admits_system_group_capability(monkeypatch):
    """``system.*`` caps have no project dimension to violate — they stay
    admissible from a group row exactly as before, even with NO project
    role at all (deployment-wide router-admin verbs)."""
    from agent_mcp.core.capabilities import resolve_capabilities

    _patch_group_caps(monkeypatch, {"system.projects.manage"})

    caps = resolve_capabilities(
        user_id="mallory",
        agent_id=None,
        sysadmin=False,
        agent_role=None,
        project_role=None,
        kind="operator_session",
    )

    assert "system.projects.manage" in caps


# ── (2) live-exploit shape: viewer + unrelated group resource cap can no
#        longer mutate via the /mcp-reachable tool ──────────────────────


@pytest.mark.asyncio
async def test_viewer_with_unrelated_group_memories_create_denied(
    tmp_path, monkeypatch
) -> None:
    """The confirmed live repro: a viewer-tier caller, whose only route to
    ``memories.create`` is a group grant with no project dimension,
    must be denied by ``create_project_context`` — the ``/mcp``-reachable
    tool LIVE-EXPLOITED in R2-F3.

    RED on main: ``Ok`` (the memory is created). GREEN: ``PermissionDenied``.
    """
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            create_project_context_tool_impl,
        )

        principal = _viewer_principal_with_group_caps(
            monkeypatch, {"memories.create"}
        )

        result = await create_project_context_tool_impl(
            {"context_key": "r2f3-exploit", "context_value": "pwned"},
            principal=principal,
        )

        assert isinstance(result, PermissionDenied), (
            f"expected PermissionDenied, got {result!r}"
        )

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM project_context WHERE context_key = ?",
                ("r2f3-exploit",),
            ).fetchone()
        finally:
            conn.close()
        assert row is None, "denied create must not leave a row behind"


@pytest.mark.asyncio
async def test_viewer_without_group_cap_also_denied_control(
    tmp_path, monkeypatch
) -> None:
    """Positive/negative control isolating the cause: revoke the group
    capability (grant nothing) and repeat the identical call — it must
    be denied exactly as it was before the group grant existed, and
    exactly as it is after the fix. Pins that the denial isn't an
    accidental side effect of some OTHER change."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            create_project_context_tool_impl,
        )

        principal = _viewer_principal_with_group_caps(monkeypatch, set())

        result = await create_project_context_tool_impl(
            {"context_key": "r2f3-control", "context_value": "nope"},
            principal=principal,
        )

        assert isinstance(result, PermissionDenied), (
            f"expected PermissionDenied, got {result!r}"
        )


@pytest.mark.asyncio
async def test_project_membership_role_still_grants_resource_cap(
    tmp_path,
) -> None:
    """The CORRECT, already project-scoped delegation path is untouched:
    an operator-tier caller whose ``memories.create`` comes from
    ``project_membership.role`` (``PROJECT_ROLE_BUNDLES["operator"]``, no
    group involved at all) still succeeds."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            create_project_context_tool_impl,
        )

        principal = make_principal(
            kind="operator_session",
            user_id="legit-operator",
            agent_id=None,
            sysadmin=False,
            project_name="project-a",
            project_role="operator",
            agent_role=None,
            can_wake_loop=False,
            source_token=None,
        )
        assert "memories.create" in PROJECT_ROLE_BUNDLES["operator"]

        result = await create_project_context_tool_impl(
            {"context_key": "r2f3-legit", "context_value": "fine"},
            principal=principal,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"


# ── (3) class-sweep: same chokepoint closes the leak on a sibling gate ──


@pytest.mark.asyncio
async def test_viewer_with_unrelated_group_tasks_delete_denied(
    monkeypatch,
) -> None:
    """Sibling tool, decorator-shaped gate (``@requires_capability``)
    instead of the project-context module's in-body
    ``_deny_viewer_tier_write`` — proves the fix is centralized at
    ``resolve_capabilities`` rather than needing a per-tool patch.

    RED on main: the decorator admits and the call proceeds into the
    task-lookup body (no ``AuthRejected``). GREEN: ``AuthRejected``."""
    from agent_mcp.tools.task_tools import delete_task_tool_impl

    principal = _viewer_principal_with_group_caps(monkeypatch, {"tasks.delete"})

    with pytest.raises(AuthRejected):
        await delete_task_tool_impl(
            {"task_id": "does-not-matter"}, principal=principal
        )


def test_sysadmin_wildcard_still_bypasses_everything(monkeypatch):
    """Unrelated to the group-cap path entirely, but a cheap belt-and-
    braces check that the fix's new filtering logic doesn't accidentally
    touch the sysadmin short-circuit."""
    from agent_mcp.core.capabilities import resolve_capabilities

    caps = resolve_capabilities(
        user_id="root",
        agent_id=None,
        sysadmin=True,
        agent_role=None,
        project_role=None,
        kind="operator_session",
    )
    assert caps == frozenset({SYSADMIN_WILDCARD})
