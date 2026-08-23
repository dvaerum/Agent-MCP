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
# backlog. It is now EMPTY: every registered tool declares its
# requirement where the registry can see it. Keep it that way — the
# assertion below is an exact-set comparison, so re-adding a name here
# is a deliberate, reviewable act rather than a silent regression.
UNSTAMPED_MIGRATION_GAPS: frozenset[str] = frozenset()


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


# ── register_tool(requires=...) — the declaration itself ─────────────
#
# The sweep above proves every IMPL carries a gate. These prove the
# CATALOGUE can't be fed a tool without one: ``requires=`` has no
# default, and a declaration that contradicts the impl's live stamp is
# an ImportError rather than a silent lie (the drift class
# ``tools/access.py``'s module docstring warns about).


async def _ungated_impl(arguments, *, principal=None):  # pragma: no cover
    raise AssertionError("never invoked — registration must fail first")


_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def test_register_tool_requires_the_declaration() -> None:
    """Omitting ``requires=`` is a TypeError, not a default."""
    from agent_mcp.tools.registry import register_tool

    with pytest.raises(TypeError, match="requires"):
        register_tool(
            name="_probe_missing_requires",
            description="probe",
            input_schema=_SCHEMA,
            implementation=_ungated_impl,
        )


def test_register_tool_rejects_a_capability_the_impl_does_not_enforce() -> None:
    """Claiming a cap an undecorated impl doesn't enforce fails at import."""
    from agent_mcp.core.authorize import Cap
    from agent_mcp.tools.registry import register_tool

    with pytest.raises(ValueError, match="no @requires_. decorator"):
        register_tool(
            name="_probe_lying_cap",
            description="probe",
            input_schema=_SCHEMA,
            implementation=_ungated_impl,
            requires=Cap("system.config.write"),
        )


def test_register_tool_rejects_a_mismatched_capability() -> None:
    """Declaring cap A while the impl enforces cap B fails at import."""
    from agent_mcp.core.authorize import Cap, requires_capability
    from agent_mcp.tools.registry import register_tool

    gated = requires_capability("tasks.view")(_ungated_impl)
    with pytest.raises(ValueError, match="tasks.delete"):
        register_tool(
            name="_probe_wrong_cap",
            description="probe",
            input_schema=_SCHEMA,
            implementation=gated,
            requires=Cap("tasks.delete"),
        )


def test_register_tool_rejects_public_on_a_gated_impl() -> None:
    """PUBLIC cannot be used to hide an enforced gate from the catalogue."""
    from agent_mcp.core.authorize import PUBLIC, requires_capability
    from agent_mcp.tools.registry import register_tool

    gated = requires_capability("tasks.view")(_ungated_impl)
    with pytest.raises(ValueError, match="PUBLIC"):
        register_tool(
            name="_probe_public_lie",
            description="probe",
            input_schema=_SCHEMA,
            implementation=gated,
            requires=PUBLIC,
        )


def test_register_tool_rejects_a_mismatched_predicate_reason() -> None:
    """A Predicate declaration whose reason drifts from the decorator's
    fails at import — that string reaches agent transcripts, so the two
    copies must stay identical."""
    from agent_mcp.core.authorize import Predicate, requires_predicate
    from agent_mcp.tools.registry import register_tool

    gated = requires_predicate(lambda p: p is not None, "Unauthorized: nope")(
        _ungated_impl
    )
    with pytest.raises(ValueError, match="differs"):
        register_tool(
            name="_probe_drifted_reason",
            description="probe",
            input_schema=_SCHEMA,
            implementation=gated,
            requires=Predicate("Unauthorized: something else"),
        )


# ── visibility= shrink invariant ─────────────────────────────────────


def _visibility_kwarg_sites():
    """Yield ``(tool_name, declared_visibility)`` for every registration
    that still passes an explicit ``visibility=`` kwarg."""
    import ast
    import pathlib

    import agent_mcp.tools as _tools_pkg

    pkg_dir = pathlib.Path(_tools_pkg.__file__).parent
    for path in sorted(pkg_dir.glob("*.py")):
        if path.name == "registry.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "register_tool"
            ):
                continue
            kw = {k.arg: k for k in node.keywords}
            if "visibility" not in kw:
                continue
            name_node = kw["name"].value if "name" in kw else node.args[0]
            yield name_node.value, path.name


def test_visibility_kwarg_only_survives_where_it_does_real_work() -> None:
    """``visibility=`` must not restate a tier the live gate already
    derives.

    Per N4 the kwarg cannot go to zero — a ``@requires_predicate`` tool
    maps to no tier (``core/authorize.requires_predicate`` explains why),
    so it is the only ``tools/list`` signal those tools have. What it
    CAN stop being is a hand-synced echo of a derivable tier, which is
    the two-sources-of-truth shape ``tools/access.py``'s docstring warns
    about. This test allows exactly two reasons to keep it:

    1. the tool is ``@requires_predicate``-gated (no derivable tier), or
    2. the kwarg strictly TIGHTENS the cap-derived tier (a deliberate
       UX choice — e.g. ``create_task`` carries a worker-tier cap but is
       an admin-orchestration surface).
    """
    from agent_mcp.tools.access import _LEVEL_RANK, _visibility_for_capability

    offenders = []
    for name, module in _visibility_kwarg_sites():
        entry = tool_registry.get(name)
        assert entry is not None, f"{name} ({module}) is not registered"
        impl = entry.meta.implementation
        if getattr(impl, "_required_predicate", None) is not None:
            continue  # reason 1
        cap = getattr(impl, "_required_capability", None)
        policy_keys = getattr(impl, "_required_policy_keys", None)
        declared = entry.meta.declared_visibility
        if cap is not None:
            derived = _visibility_for_capability(cap)
            declared_rank = _LEVEL_RANK.get(declared)
            if declared_rank is not None and declared_rank < _LEVEL_RANK[derived]:
                continue  # reason 2
            offenders.append((name, module, declared, derived))
        elif policy_keys:
            offenders.append(
                (name, module, declared, "worker-if-toggled:" + ",".join(policy_keys))
            )
        else:
            continue  # PUBLIC — the kwarg is its only signal too
    assert not offenders, (
        "register_tool(visibility=...) restates a tier the live gate "
        f"already derives; drop the kwarg: {offenders}"
    )
