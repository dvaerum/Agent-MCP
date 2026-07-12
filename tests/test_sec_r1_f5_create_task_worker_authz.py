"""SECURITY (pentest R1-F5): ``create_task`` is registered with
``visibility="operator"`` (hidden from a worker's ``tools/list``), but
before this fix the impl was gated ONLY by
``@requires_capability("tasks.create")`` — and ``tasks.create`` is ALSO
in ``AGENT_ROLE_BUNDLES["worker"]`` (``core/capabilities.py``).
Dispatch routes by tool NAME and runs only the capability decorator
(``tools/registry.py``); ``visibility`` governs ``tools/list``
filtering ONLY, never authorization. So a worker bearer — for whom
``create_task`` is invisible in ``tools/list`` — could still
``tools/call create_task`` by name and get operator behavior the
worker-facing paths forbid:

* a ROOT task (``create_self_task`` blocks root creation for non-admin
  callers; ``create_task`` had no such guard), and
* an arbitrary ``assigned_to`` (the worker ``assign_task`` path denies
  cross-agent assignment; ``create_task`` had no such guard either).

Live-proven during the pentest: a worker principal created a root task
and a task assigned to a different agent via ``create_task``.

The fix adds an in-body tier gate at the top of
``create_task_tool_impl``: a caller lacking ``tasks.assign`` (the same
"is_admin_request" predicate every other worker/operator split in
``task_tools.py`` already uses) is denied and pointed at
``create_self_task``. Operator (``PROJECT_ROLE_BUNDLES["operator"]``),
manager (``AGENT_ROLE_BUNDLES["manager"]``), and sysadmin (wildcard)
all carry/short-circuit ``tasks.assign``, so legitimate use is
unaffected.

This file is RED against the pre-fix code (worker calls succeed) and
GREEN after. It also carries the class-sweep coupling test: every
registered tool whose derived ``tools/list`` visibility is
``"operator"`` (hidden from worker *and* manager) but whose
``@requires_capability`` cap is one a worker principal actually holds
is either an explicitly-documented ownership-gated exception
(``bulk_task_operations``) or must be proven, here, to deny a
worker-tier caller — so a FUTURE ``create_task``-shaped drift fails
loudly instead of shipping silently.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


def _set_agent_role(token: str, agent_role: str) -> None:
    """Flip an existing agent row's ``agent_role`` + sync the cache.

    Same helper ``test_phase2_wave3_permission_matrix.py`` uses to
    promote a harness-created worker to manager-tier without a second
    registration path.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agents SET agent_role = ? WHERE token = ?",
            (agent_role, token),
        )
        conn.commit()
    finally:
        conn.close()
    if token in g.active_agents:
        g.active_agents[token]["agent_role"] = agent_role


# --- RED -> GREEN: worker-tier create_task denial ---------------------


@pytest.mark.asyncio
async def test_worker_cannot_create_root_task_via_create_task(
    tmp_path,
) -> None:
    """A worker bearer must be denied ``create_task`` for a root task.

    Pre-fix this succeeded and minted a root (parent=None) task — the
    exact escalation ``create_self_task`` was designed to block.
    """
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-root-escalation")

        await worker.assert_unauthorized(
            "create_task",
            {"task_title": "worker-forged root task"},
        )


@pytest.mark.asyncio
async def test_worker_cannot_create_task_assigned_to_other_agent(
    tmp_path,
) -> None:
    """A worker bearer must be denied ``create_task`` targeting another
    agent's ``assigned_to`` — the cross-agent-assignment escalation the
    ``assign_task`` worker path explicitly forbids.
    """
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-assign-escalation")
        victim = await admin.create_worker("victim-agent")

        await worker.assert_unauthorized(
            "create_task",
            {
                "task_title": "worker-forged assignment",
                "assigned_to": victim.agent_id,
            },
        )


@pytest.mark.asyncio
async def test_worker_denial_message_points_to_create_self_task(
    tmp_path,
) -> None:
    """The denial should name the intended worker path, not just deny."""
    from tests.harness import _first_text

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-message-check")

        result = await worker.call(
            "create_task", {"task_title": "should be denied"}
        )
        text = _first_text(result)
        assert "create_self_task" in text, (
            f"denial message should point workers at create_self_task; "
            f"got {text!r}"
        )


# --- No regression: operator / manager / sysadmin still succeed -------


@pytest.mark.asyncio
async def test_manager_agent_bearer_can_still_create_task(tmp_path) -> None:
    """A plain (non-sysadmin) manager-tier agent bearer must still be
    able to call ``create_task`` — the fix must not over-restrict past
    the worker tier.
    """
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-create-task")
        _set_agent_role(mgr.token, "manager")

        result = await mgr.assert_tool_succeeds(
            "create_task", {"task_title": "manager-created task"}
        )
        assert result, "manager create_task should return content"


@pytest.mark.asyncio
async def test_operator_session_create_task_still_succeeds(tmp_path) -> None:
    """The REST ``POST /api/tasks`` operator-session path (which shares
    ``create_task_tool_impl`` — see ``test_create_task_one_path.py``)
    must still succeed after the tier gate.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "operator-created task",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True, r.json()


@pytest.mark.asyncio
async def test_sysadmin_agent_bearer_can_still_create_root_task(
    tmp_path,
) -> None:
    """The harness's admin bearer (agent_bearer + sysadmin=True) must
    still be able to create a root task — sysadmin's wildcard cap must
    short-circuit the new ``tasks.assign`` gate exactly like it
    short-circuits every other capability check.
    """
    async with mcp_session(tmp_path) as admin:
        result = await admin.assert_tool_succeeds(
            "create_task", {"task_title": "sysadmin-created root task"}
        )
        assert result


# --- Class-sweep: pin the visibility != enforcement invariant ---------

#: Tools whose derived ``tools/list`` visibility is tighter than what
#: their ``@requires_capability`` decorator would ADMIT for a
#: worker-tier caller (i.e. the exact R1-F5 shape: hidden from
#: tools/list, but not actually denied at the enforcement layer by the
#: decorator alone), yet are intentionally still worker-CALLABLE
#: because an in-body ownership/scope gate constrains what a worker
#: can do with them. Every entry needs a documented WHY; a bare
#: "it's fine" is not acceptable here — this dict is the class's only
#: escape hatch.
_OWNERSHIP_GATED_EXCEPTIONS: dict[str, str] = {
    "bulk_task_operations": (
        "in-body ownership gate (task_tools.py, 'Permission check' / "
        "PF-1 comment): every operation is rejected unless "
        "task.assigned_to == the calling agent OR the caller holds "
        "tasks.assign (operator/manager) -- a worker can only mutate "
        "ITS OWN tasks through this tool, never another agent's, and "
        "cannot create/delete/reassign, so the operator-tier "
        "'orchestration surface' framing in the tools/list hide holds "
        "even though the decorator alone would admit a worker call."
    ),
}

#: Minimal MCP arguments that prove a worker-tier caller is denied for
#: each tool in the mismatch class NOT covered by
#: ``_OWNERSHIP_GATED_EXCEPTIONS``. A tool newly entering the mismatch
#: class must be added here (with a passing denial) or to the
#: exceptions dict above (with a documented ownership gate) — the
#: coupling test below fails loudly otherwise.
_WORKER_DENIAL_FIXTURES: dict[str, dict] = {
    "create_task": {"task_title": "class-sweep coupling probe"},
}


def _tier_mismatched_tools() -> dict[str, str]:
    """``{tool_name: cap}`` for every registered tool whose derived
    ``tools/list`` visibility is ``"operator"`` (hidden from BOTH
    worker and manager) but whose ``@requires_capability`` cap is one
    a worker-tier principal actually carries
    (``AGENT_ROLE_BUNDLES["worker"]``). This is the exact R1-F5 shape:
    visibility says "workers can't see this"; the live enforcement gate
    disagrees.
    """
    import agent_mcp.tools  # noqa: F401 -- registers every tool as an import side effect
    from agent_mcp.core.capabilities import AGENT_ROLE_BUNDLES
    from agent_mcp.tools.access import TOOL_ACCESS
    from agent_mcp.tools.registry import tool_registry

    worker_caps = AGENT_ROLE_BUNDLES["worker"]
    out: dict[str, str] = {}
    for name in tool_registry.names():
        entry = tool_registry.get(name)
        if entry is None:  # pragma: no cover -- names()/get() agree
            continue
        if TOOL_ACCESS.get(name) != "operator":
            continue
        cap = getattr(entry.meta.implementation, "_required_capability", None)
        if cap in worker_caps:
            out[name] = cap
    return out


def test_tier_mismatch_class_is_fully_classified() -> None:
    """Every tool in the R1-F5 mismatch class must be either the
    documented ownership-gated exception or have a worker-denial
    fixture below. A tool joining the class unclassified means a new
    ``create_task``-shaped escalation shipped — fail loudly, don't let
    it slide through silently the way ``create_task`` originally did.
    """
    mismatches = _tier_mismatched_tools()
    known = set(_OWNERSHIP_GATED_EXCEPTIONS) | set(_WORKER_DENIAL_FIXTURES)
    unclassified = set(mismatches) - known
    assert not unclassified, (
        f"new operator-visibility/worker-cap tool(s) {sorted(unclassified)} "
        "are unclassified -- add to _OWNERSHIP_GATED_EXCEPTIONS (with a "
        "documented in-body ownership gate) or _WORKER_DENIAL_FIXTURES "
        "(with args proving a worker-tier caller is denied) in "
        "tests/test_sec_r1_f5_create_task_worker_authz.py"
    )
    # Also pin the CURRENT membership (not just "unclassified is empty")
    # so a tool silently DROPPING off the mismatch list (e.g. because its
    # cap decorator or visibility kwarg quietly changed) is visible too --
    # a shrinking set is safe, but if it happens by accident it should be
    # visible in review as an intentional decision, not a diff nobody
    # runs into. If this fails because a new tool was added to
    # _WORKER_DENIAL_FIXTURES / _OWNERSHIP_GATED_EXCEPTIONS in
    # anticipation of an upcoming tool, update the expectation set too.
    assert set(mismatches) == known, (
        f"mismatch class membership changed: now={sorted(mismatches)} "
        f"expected={sorted(known)} -- update the classification dicts "
        "in this file to match"
    )


@pytest.mark.asyncio
async def test_tier_mismatched_tools_deny_worker_callers(tmp_path) -> None:
    """For every mismatch-class tool NOT covered by the documented
    ownership-gated exception, prove a worker-tier caller is actually
    denied at runtime -- not just "the decorator would admit it and we
    hope the in-body check catches it".
    """
    mismatches = _tier_mismatched_tools()
    to_prove = set(mismatches) - set(_OWNERSHIP_GATED_EXCEPTIONS)

    async with mcp_session(tmp_path) as admin:
        for name in sorted(to_prove):
            args = _WORKER_DENIAL_FIXTURES.get(name)
            assert args is not None, (
                f"tool {name!r} is in the R1-F5 mismatch class but has no "
                f"entry in _WORKER_DENIAL_FIXTURES -- add one so this test "
                f"can prove worker denial"
            )
            worker = await admin.create_worker(f"w-classsweep-{name}")
            await worker.assert_unauthorized(name, args)
