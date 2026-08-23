"""Arch invariant (security-hardening Phase 2, Finding A): every
registered MCP tool declares its authorization requirement where the
registry can see it.

Why this exists
---------------
The recurring bug lineage this whole hardening plan targets (OBS-R11-1,
rediscovered 15+ times across ``pentest-all``'s 21 rounds) is
"opt-in-and-forget" authorization: a tool ships with its capability
check written by hand *inside* the tool body, so nothing outside the
body knows the tool is gated. Two concrete consequences:

* ``dispatch_tool_call``'s pre-schema authorization gate (R20-F4 /
  R21-F1, ``tools/registry.py``) can only fire for a tool whose
  requirement is *stamped on the implementation object*
  (``_required_capability`` / ``_required_policy_keys`` /
  ``_required_predicate``). An in-body-only tool reaches
  ``jsonschema.validate`` first, leaking its exact schema shape to a
  caller who was never going to be allowed to invoke it.
* ``tools/access.py``'s ``tools/list`` visibility derivation falls back
  to the hand-synced ``visibility=`` kwarg, which can silently disagree
  with the real gate.

This module is the self-discovering guard: it walks the LIVE registry
(no hand-maintained tool list) and fails when a tool carries no stamped
requirement and is not on the small, explicit capability-free
allowlist. A new tool added without an authorization declaration reds
this test the moment its module is imported.

Companion invariant: :func:`test_derived_access_levels_are_pinned`
freezes the ``tools/list`` tier every tool derives today, so the
migration (and the later ``visibility=`` shrink) cannot change WHO sees
WHAT by accident.
"""
from __future__ import annotations

import pytest

import agent_mcp.tools  # noqa: F401 — import for side effect: registers tools
from agent_mcp.tools.access import TOOL_ACCESS
from agent_mcp.tools.registry import tool_registry

# Tools that legitimately carry NO authorization requirement. Keep this
# set tiny and justified — each entry is a deliberate decision that the
# tool is safe for an entirely unauthenticated caller.
#
# ``test``: a fixed "Tool is working!" string. Takes no arguments,
# reads nothing, writes nothing. It is the MCP connectivity probe, so
# gating it would make "can I reach the server at all?" untestable
# without credentials.
CAPABILITY_FREE_TOOLS = frozenset({"test"})


# Tools still gated ONLY by an in-body check — the Phase 2 migration
# backlog. This set must SHRINK, never grow: each file's migration PR
# removes its tools from here. The assertion below is an exact-set
# comparison (not a subset) so a newly-added ungated tool cannot hide
# inside a stale allowlist.
UNSTAMPED_MIGRATION_GAPS = frozenset(
    {
        # agent_communication_tools.py
        "fetch_events_since",
        "get_agent_messages",
        "send_agent_message",
        "wait_for_events",
    }
)


def declared_requirement(impl):
    """Return a ``(kind, detail)`` description of ``impl``'s stamped
    authorization requirement, or ``None`` when it carries none.

    Reads exactly the three attributes ``dispatch_tool_call``'s
    pre-schema gate consults, so "discoverable by this test" and
    "enforced before schema validation" cannot drift apart.
    """
    cap = getattr(impl, "_required_capability", None)
    if cap is not None:
        return ("capability", cap)
    policy_keys = getattr(impl, "_required_policy_keys", None)
    if policy_keys:
        return ("policy", tuple(policy_keys))
    predicate = getattr(impl, "_required_predicate", None)
    if predicate is not None:
        return (
            "predicate",
            getattr(impl, "_required_predicate_reason", "Unauthorized"),
        )
    return None


def _registered_tools():
    for name in sorted(tool_registry.names()):
        entry = tool_registry.get(name)
        if entry is None:  # pragma: no cover — names()/get() agree
            continue
        yield name, entry


def test_registry_is_populated() -> None:
    """Guard the guard: an empty registry would pass every sweep below
    vacuously."""
    assert len(list(_registered_tools())) >= 40


@pytest.mark.parametrize("name", sorted(n for n, _ in _registered_tools()))
def test_tool_declares_an_authorization_requirement(name: str) -> None:
    """Each registered tool either carries a stamped requirement, is on
    the capability-free allowlist, or is a known un-migrated gap."""
    entry = tool_registry.get(name)
    assert entry is not None
    requirement = declared_requirement(entry.meta.implementation)
    if requirement is not None:
        return
    assert name in CAPABILITY_FREE_TOOLS or name in UNSTAMPED_MIGRATION_GAPS, (
        f"tool {name!r} has no stamped authorization requirement. Declare "
        "it with @requires_capability / @requires_policy / "
        "@requires_predicate on the implementation (an in-body check is "
        "invisible to dispatch_tool_call's pre-schema gate and to "
        "tools/access.py), or add it to CAPABILITY_FREE_TOOLS with a "
        "written justification."
    )


def test_unstamped_set_matches_the_declared_backlog_exactly() -> None:
    """The set of ungated tools is EXACTLY the declared backlog.

    Exact-set (not subset) on purpose: a newly added ungated tool must
    not be able to hide behind a stale allowlist, and a migrated tool
    must be struck from the backlog in the same PR that migrates it.
    """
    unstamped = {
        name
        for name, entry in _registered_tools()
        if declared_requirement(entry.meta.implementation) is None
    }
    assert unstamped - CAPABILITY_FREE_TOOLS == UNSTAMPED_MIGRATION_GAPS


def test_capability_free_allowlist_has_no_stale_entries() -> None:
    """Every allowlisted tool is still registered and still ungated."""
    registered = {name for name, _ in _registered_tools()}
    assert CAPABILITY_FREE_TOOLS <= registered
    for name in CAPABILITY_FREE_TOOLS:
        entry = tool_registry.get(name)
        assert declared_requirement(entry.meta.implementation) is None, (
            f"{name!r} is on the capability-free allowlist but now carries "
            "a requirement — remove it from CAPABILITY_FREE_TOOLS."
        )


# ── tools/list tier snapshot ─────────────────────────────────────────
#
# Stamping a requirement changes what ``tools/access._derive_access_level``
# can SEE, so it can change a tool's advertised tier. Freezing the whole
# map here means any such change has to be a deliberate edit to this
# dict, reviewed alongside the migration that caused it — never a silent
# side effect. (It also pins the final ``visibility=`` shrink: deleting a
# redundant kwarg must leave every tier byte-identical.)
EXPECTED_ACCESS_LEVELS = {
    "add_task_note": "any",
    "ask_project_rag": "any",
    "assign_task": (
        "worker-if-toggled:config_allow_worker_self_assign,"
        "config_allow_worker_create_unassigned"
    ),
    "backup_project_context": "operator",
    "broadcast_admin_message": "operator",
    "bulk_task_operations": "operator",
    "bulk_update_project_context": "any",
    "check_file_status": "any",
    "create_project_context": "any",
    "create_scheduled_directive": (
        "worker-if-toggled:config_allow_worker_self_schedule,"
        "config_allow_manager_curate_schedules"
    ),
    "create_self_task": "worker",
    "create_task": "operator",
    "delete_project_context": "any",
    "delete_project_settings": "operator",
    "delete_scheduled_directive": (
        "worker-if-toggled:config_allow_worker_self_schedule,"
        "config_allow_manager_curate_schedules"
    ),
    "delete_task": "operator",
    "delete_task_note": "any",
    "edit_agent": "operator",
    "edit_task_note": "any",
    "fetch_events_since": "any",
    "get_agent_messages": "any",
    "get_agent_tokens": "operator",
    "get_system_prompt": "worker",
    "list_scheduled_directives": "worker",
    "purge_agent": "operator",
    "register_agent": "operator",
    "request_assistance": "worker",
    "restore_agent": "operator",
    "search_tasks": "worker",
    "send_agent_message": "worker-if-toggled:config_allow_worker_to_worker",
    "terminate_agent": "operator",
    "test": "any",
    "update_agent_profile": "worker",
    "update_file_metadata": "operator",
    "update_file_status": "any",
    "update_project_context": "any",
    "update_project_settings": "operator",
    "update_scheduled_directive": (
        "worker-if-toggled:config_allow_worker_self_schedule,"
        "config_allow_manager_curate_schedules"
    ),
    "update_task": "operator",
    "update_task_status": "worker-if-toggled:config_allow_worker_update_own_status",
    "validate_context_consistency": "any",
    "view_agents": "worker",
    "view_audit_log": "operator",
    "view_file_metadata": "any",
    # Phase 2 (Finding A) DELIBERATE tier change, the only one in the
    # whole migration: stamping ``@requires_capability("memories.view")``
    # lets the derivation see the live cap, which sits in the worker
    # bundle -> "worker" (was the ``visibility=`` default "any"). No
    # policy change -- the cap gate ALREADY rejected anonymous callers,
    # who hold no capabilities; the tool merely stops being advertised
    # to a caller that could never invoke it. This is exactly the leak
    # ``test_tool_visibility_capability_coupling`` was written to force.
    "view_project_context": "worker",
    "view_project_settings": "operator",
    "view_status": "operator",
    "view_tasks": "worker",
    "wait_for_events": "any",
}


def test_derived_access_levels_are_pinned() -> None:
    """The derived ``tools/list`` tier of every registered tool matches
    the frozen snapshot above."""
    assert TOOL_ACCESS() == EXPECTED_ACCESS_LEVELS
