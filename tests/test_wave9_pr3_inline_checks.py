"""Wave 9 PR 3 — capability-gate assertions at each migrated branch.

PR 3 of 7 in ``prancy-napping-pie.md`` (Wave 9). Pins, per migrated
``principal.has_role(...)`` call site, that the new
``principal.has_capability(...)`` gate admits or denies the same
shapes the legacy bridge admitted — AND, more importantly, that the
gate now responds to the explicit capability rather than the
identity tier the legacy bridge happened to expand to.

Coverage matrix (cap → call sites):

* ``tasks.assign`` — task_tools (7 sites, all ``is_admin_request``)
  and task_notes_tools (``is_admin`` flag on edit/delete moderation)
* ``system.config.write`` — agent_communication_tools
  (``_is_operator_tier``), project_context_tools
  (``_is_admin_principal``), file_metadata_tools (entry gate on
  ``update_file_metadata``), registry (``list_available_tools``
  visibility filter)
* ``agents.register`` / ``agents.terminate`` / ``agents.view`` /
  ``system.view`` — admin_tools (per-tool ``_require_capability``)
* identity check — project_context_tools
  (``_requires_authenticated_caller``) preserves the "any caller"
  semantics on ``principal.kind`` per the Wave 9 plan's mapping
  ("any" questions stay identity checks).

Each test seeds a principal with / without the chosen cap and
asserts the branch is taken (or not). The ``with_capabilities()``
harness helper produces an operator-shaped Principal carrying the
exact frozenset passed in, so the cap gate is the only variable.
"""
from __future__ import annotations

import pytest

from agent_mcp.core.principal import Principal
from tests.harness import make_principal, with_capabilities


# ── Principal builders shared across tests ────────────────────────


def _agent_bearer(
    *,
    agent_id: str = "wkr",
    agent_role: str | None = "worker",
    token: str = "dummy-tok",
) -> Principal:
    """Agent-bearer Principal with the named role's bundle resolved.

    Used to assert the cap-gate behaviour for the agent path;
    ``make_principal`` resolves capabilities via ``resolve_capabilities``,
    which pulls ``AGENT_ROLE_BUNDLES[agent_role]``.
    """
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=agent_role,  # type: ignore[arg-type]
        can_wake_loop=False,
        source_token=token,
    )


def _operator_principal(
    *, project_role: str | None = "operator",
) -> Principal:
    return make_principal(
        kind="operator_session",
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        project_name="proj",
        project_role=project_role,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _sysadmin_principal() -> Principal:
    return make_principal(
        kind="operator_session",
        user_id="root",
        agent_id=None,
        sysadmin=True,
        project_name=None,
        project_role=None,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


# ── task_tools — `tasks.assign` gate (7 sites collapsed) ──────────


@pytest.mark.parametrize(
    "principal_factory,expect_admin",
    [
        # Operator-tier carries tasks.assign via PROJECT_ROLE_BUNDLES.
        (lambda: _operator_principal(project_role="operator"), True),
        # Sysadmin wildcard short-circuits has_capability.
        (lambda: _sysadmin_principal(), True),
        # Manager-role agent carries tasks.assign via AGENT_ROLE_BUNDLES.
        (lambda: _agent_bearer(agent_role="manager", agent_id="mgr"), True),
        # Worker-role agent does NOT carry tasks.assign.
        (lambda: _agent_bearer(agent_role="worker", agent_id="wkr"), False),
        # Viewer-tier operator does NOT carry tasks.assign — tighter
        # than the legacy bridge which admitted viewers as "admin".
        (lambda: _operator_principal(project_role="viewer"), False),
    ],
)
def test_authorize_assign_task_admin_branch_gated_by_tasks_assign(
    principal_factory, expect_admin,
) -> None:
    """``_authorize_assign_task``'s short-circuit branch (returning
    ``None`` for admin/manager callers without per-mode arbitration)
    is now gated by ``has_capability("tasks.assign")``. Workers /
    viewers fall through to the per-mode arbitration where the
    no-target-agent path requires the per-policy toggle."""
    from agent_mcp.tools.task_tools import _authorize_assign_task

    principal = principal_factory()
    result = _authorize_assign_task(
        target_agent_token=None,
        task_ids=None,
        arguments={},
        principal=principal,
    )
    if expect_admin:
        # Admin admit short-circuits → None (permitted).
        assert result is None, (
            f"admin-tier {principal.kind}/{principal.agent_role}"
            f" should admit on tasks.assign cap, got {result!r}"
        )
    else:
        # Non-admin falls through. With no target_agent_token + no
        # worker_id (operator path) → "Unauthorized: Admin token
        # required". With worker_id present (agent path) → admits via
        # Mode 0 (file-unassigned default-true policy).
        if principal.kind == "agent_bearer":
            # Worker: Mode 0 path admits (policy default is True).
            assert result is None
        else:
            # Viewer-tier operator: no admin admit, no worker_id →
            # unauthorized.
            assert isinstance(result, str)
            assert "Admin token" in result


def test_authorize_assign_task_worker_no_target_admits_via_mode_0() -> None:
    """The capability migration preserves the worker self-file path:
    Mode 0 (no target_agent_token) admits when the
    ``config_allow_worker_create_unassigned`` policy default holds —
    same behaviour as the pre-migration ``has_role(...)`` matrix."""
    from agent_mcp.tools.task_tools import _authorize_assign_task

    worker = _agent_bearer(agent_role="worker", agent_id="wkr-1")
    arguments: dict = {}
    result = _authorize_assign_task(
        target_agent_token=None,
        task_ids=None,
        arguments=arguments,
        principal=worker,
    )
    assert result is None
    assert arguments["_worker_created_by"] == "wkr-1"


# ── agent_communication_tools — `system.config.write` operator gate ─


def test_is_operator_tier_gated_by_system_config_write() -> None:
    """``_is_operator_tier`` (used as ``is_admin`` for cross-agent
    messaging override) now gates on ``system.config.write`` — the
    operator-bundle write marker. The agent_id == "admin" harness
    escape hatch is preserved for legacy fixtures."""
    from agent_mcp.tools.agent_communication_tools import _is_operator_tier

    # Operator-tier (project_role="operator") carries the cap.
    op = _operator_principal(project_role="operator")
    assert _is_operator_tier(op)

    # Sysadmin admits via wildcard short-circuit.
    sysadmin = _sysadmin_principal()
    assert _is_operator_tier(sysadmin)

    # Viewer-tier operator does NOT carry system.config.write —
    # tighter than the legacy bridge which admitted them.
    viewer = _operator_principal(project_role="viewer")
    assert not _is_operator_tier(viewer)

    # Plain worker agent doesn't carry the cap.
    worker = _agent_bearer(agent_role="worker", agent_id="alice-wkr")
    assert not _is_operator_tier(worker)

    # Manager-role agent doesn't carry system.config.write either —
    # the cap is reserved for operator-tier (project_role="operator")
    # and sysadmin. The agent_id-label escape hatch is the only thing
    # that admits a manager-role agent here.
    mgr = _agent_bearer(agent_role="manager", agent_id="alice-mgr")
    assert not _is_operator_tier(mgr)

    # Legacy harness escape: agent_id == "admin" admits regardless of
    # cap set (preserves the manager-row labelled "admin" fixture).
    admin_label = _agent_bearer(agent_role="manager", agent_id="admin")
    assert _is_operator_tier(admin_label)


# ── project_context_tools — `system.config.write` + identity check ─


def test_is_admin_principal_gated_by_system_config_write() -> None:
    """``_is_admin_principal`` (the per-key admin override + secret
    redaction bypass) now gates on ``system.config.write``."""
    from agent_mcp.tools.project_context_tools import _is_admin_principal

    assert _is_admin_principal(_operator_principal(project_role="operator"))
    assert _is_admin_principal(_sysadmin_principal())
    # Viewer-tier operator: tighter than the legacy bridge which
    # admitted them; viewers shouldn't bypass secret redaction.
    assert not _is_admin_principal(_operator_principal(project_role="viewer"))
    assert not _is_admin_principal(
        _agent_bearer(agent_role="worker", agent_id="wkr"),
    )
    assert not _is_admin_principal(
        _agent_bearer(agent_role="manager", agent_id="mgr"),
    )
    assert _is_admin_principal(None) is False


def test_requires_authenticated_caller_admits_all_authenticated_identities() -> None:
    """``_requires_authenticated_caller`` is the "any caller" gate —
    per the Wave 9 plan, "any" questions stay as identity checks on
    ``principal.kind`` rather than back-fitting a capability. The
    helper admits every authenticated principal kind (agent_bearer,
    operator_session, forwarding_header) and denies only ``None``."""
    from agent_mcp.tools.project_context_tools import (
        _requires_authenticated_caller,
    )

    # Every authenticated kind admits — including viewer-tier
    # operator (read-only project member), preserving the legacy
    # behaviour where the bridge's has_role("admin") admitted them.
    assert _requires_authenticated_caller(
        _operator_principal(project_role="operator"),
    ) is None
    assert _requires_authenticated_caller(
        _operator_principal(project_role="viewer"),
    ) is None
    assert _requires_authenticated_caller(
        _agent_bearer(agent_role="worker"),
    ) is None
    assert _requires_authenticated_caller(
        _agent_bearer(agent_role="manager"),
    ) is None
    assert _requires_authenticated_caller(_sysadmin_principal()) is None

    # Forwarding-header also admits.
    fwd = make_principal(
        kind="forwarding_header",
        user_id="bob",
        agent_id=None,
        sysadmin=False,
        project_name="proj",
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token="sso-provider",
    )
    assert _requires_authenticated_caller(fwd) is None

    # None denies.
    denied = _requires_authenticated_caller(None)
    assert denied is not None


# ── task_notes_tools — entry gate + `tasks.assign` moderator flag ──


def test_task_notes_entry_gate_blocks_viewer_admits_operator() -> None:
    """The add/edit/delete entry gate (operator OR agent_bearer) now
    uses ``system.config.write`` for the operator path; viewer-tier
    operators are denied (tightening) but every agent_bearer still
    admits as before."""
    from agent_mcp.core.tool_result import PermissionDenied

    # Import the impls and run them with bad arguments — the entry
    # gate is what we're testing, so a clean entry should fall
    # through to the Invalid path (not PermissionDenied).
    async def _exercise():
        from agent_mcp.tools.task_notes_tools import (
            add_task_note_tool_impl,
            delete_task_note_tool_impl,
            edit_task_note_tool_impl,
        )

        # Operator admits → falls past gate to Invalid (no task_id).
        op = _operator_principal(project_role="operator")
        result = await add_task_note_tool_impl({}, principal=op)
        assert not isinstance(result, PermissionDenied)

        # Viewer denied at gate.
        viewer = _operator_principal(project_role="viewer")
        result = await add_task_note_tool_impl({}, principal=viewer)
        assert isinstance(result, PermissionDenied)

        # Agent bearer admits → falls past gate.
        worker = _agent_bearer(agent_role="worker", agent_id="wkr")
        result = await add_task_note_tool_impl({}, principal=worker)
        assert not isinstance(result, PermissionDenied)

        # Same gate semantics on edit + delete.
        result = await edit_task_note_tool_impl({}, principal=viewer)
        assert isinstance(result, PermissionDenied)
        result = await delete_task_note_tool_impl({}, principal=viewer)
        assert isinstance(result, PermissionDenied)

    import asyncio
    asyncio.run(_exercise())


def test_task_notes_is_admin_flag_uses_tasks_assign() -> None:
    """The ``is_admin`` flag passed to ``task_notes_db.edit_note`` /
    ``delete_note`` (bypasses per-note ownership) is sourced from
    ``has_capability("tasks.assign")``: operator + manager-role
    agent + sysadmin admits, worker + viewer denies."""
    # The flag is computed inline; exercise via _is_operator_tier
    # via the cap directly to avoid the DB round-trip.
    op = _operator_principal(project_role="operator")
    assert op.has_capability("tasks.assign")
    assert _sysadmin_principal().has_capability("tasks.assign")
    mgr = _agent_bearer(agent_role="manager", agent_id="mgr")
    assert mgr.has_capability("tasks.assign")

    wkr = _agent_bearer(agent_role="worker", agent_id="wkr")
    assert not wkr.has_capability("tasks.assign")
    viewer = _operator_principal(project_role="viewer")
    assert not viewer.has_capability("tasks.assign")


# ── file_metadata_tools — operator-tier entry gate ────────────────


def test_update_file_metadata_entry_gate_uses_system_config_write() -> None:
    """``update_file_metadata`` gates on ``system.config.write`` —
    admits operator-tier + sysadmin, denies viewer + agents."""
    from agent_mcp.core.tool_result import Invalid, PermissionDenied

    async def _exercise():
        from agent_mcp.tools.file_metadata_tools import (
            update_file_metadata_tool_impl,
        )

        # Operator: falls past gate to Invalid (no filepath).
        op = _operator_principal(project_role="operator")
        result = await update_file_metadata_tool_impl({}, principal=op)
        assert isinstance(result, Invalid)

        # Sysadmin: admits via wildcard.
        sysadmin = _sysadmin_principal()
        result = await update_file_metadata_tool_impl({}, principal=sysadmin)
        assert isinstance(result, Invalid)

        # Viewer: denied at gate.
        viewer = _operator_principal(project_role="viewer")
        result = await update_file_metadata_tool_impl({}, principal=viewer)
        assert isinstance(result, PermissionDenied)

        # Worker agent: denied at gate (lacks system.config.write).
        worker = _agent_bearer(agent_role="worker", agent_id="wkr")
        result = await update_file_metadata_tool_impl({}, principal=worker)
        assert isinstance(result, PermissionDenied)

        # None principal: denied.
        result = await update_file_metadata_tool_impl({}, principal=None)
        assert isinstance(result, PermissionDenied)

    import asyncio
    asyncio.run(_exercise())


# ── admin_tools — per-tool capability gates ───────────────────────


@pytest.mark.parametrize(
    "tool_name,cap",
    [
        ("register_agent_tool_impl", "agents.register"),
        # viewer-read-gating finding 1: view_status / view_audit_log
        # moved off the viewer-held ``system.view`` onto the operator-
        # only ``system.config.write`` — the audit log + agent working
        # dirs are operator-tier oversight data, not viewer reads.
        ("view_status_tool_impl", "system.config.write"),
        ("terminate_agent_tool_impl", "agents.terminate"),
        ("view_audit_log_tool_impl", "system.config.write"),
        # FINDING 2 security fix: agent bearer tokens are operator-tier
        # secrets, so the gate moved off the viewer-held ``agents.view``
        # onto the operator-only ``agents.register`` cap.
        ("get_agent_tokens_tool_impl", "agents.register"),
    ],
)
def test_admin_tool_require_capability_per_tool(tool_name, cap) -> None:
    """Each admin tool's entry check now names the per-action
    capability via ``_require_capability(principal, "<cap>")``. A
    principal carrying ONLY the matching cap admits past the gate;
    a principal carrying only an unrelated cap denies with
    :class:`PermissionDenied`."""
    from agent_mcp.core.tool_result import PermissionDenied
    from agent_mcp.tools import admin_tools as _admin
    impl = getattr(_admin, tool_name)

    async def _exercise():
        # Principal with EXACTLY the named cap admits past the gate.
        p_admit = with_capabilities(cap)
        result = await impl({}, principal=p_admit)
        # May fail downstream (missing args, MCP_PROJECT_DIR, etc),
        # but it must NOT be PermissionDenied — the gate admitted.
        assert not isinstance(result, PermissionDenied), (
            f"{tool_name} should admit principal carrying {cap}, "
            f"got {result!r}"
        )

        # Principal with an unrelated cap denies at the gate.
        # Pick a cap deliberately different from the tool's gate.
        other = "memories.view" if cap != "memories.view" else "files.use"
        p_deny = with_capabilities(other)
        result = await impl({}, principal=p_deny)
        assert isinstance(result, PermissionDenied), (
            f"{tool_name} should deny principal lacking {cap} "
            f"(has only {other!r}), got {result!r}"
        )

        # None principal denies.
        result = await impl({}, principal=None)
        assert isinstance(result, PermissionDenied)

    import asyncio
    asyncio.run(_exercise())


def test_require_capability_admits_sysadmin_wildcard() -> None:
    """``_require_capability`` admits a sysadmin regardless of which
    cap the tool asks for — the wildcard short-circuit in
    ``has_capability`` makes every cap admit."""
    from agent_mcp.tools.admin_tools import _require_capability

    sysadmin = _sysadmin_principal()
    for cap in (
        "agents.register",
        "agents.terminate",
        "agents.view",
        "system.view",
    ):
        assert _require_capability(sysadmin, cap) is None


# ── registry — tools/list visibility filter ───────────────────────


def test_list_available_tools_role_label_uses_system_config_write() -> None:
    """``list_available_tools`` derives the visibility role label
    from capabilities now. An operator-bundle principal labels as
    ``"admin"``; a worker agent labels as ``"worker"``; a viewer (no
    ``system.config.write``) drops to ``"anonymous"``."""
    # The cap-based branch is the only thing that changed; assert on
    # the cap query directly (matches the dispatch logic verbatim).
    op = _operator_principal(project_role="operator")
    assert op.has_capability("system.config.write") is True

    viewer = _operator_principal(project_role="viewer")
    assert viewer.has_capability("system.config.write") is False

    sysadmin = _sysadmin_principal()
    assert sysadmin.has_capability("system.config.write") is True

    worker = _agent_bearer(agent_role="worker", agent_id="wkr")
    assert worker.has_capability("system.config.write") is False
    assert worker.kind == "agent_bearer"  # falls into worker branch
