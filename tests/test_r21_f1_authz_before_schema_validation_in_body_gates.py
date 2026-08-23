"""R21-F1: 12 in-body-gated tools bypassed the R20-F4 pre-schema gate.

R20-F4 (PR #712) fixed ``dispatch_tool_call`` (agent_mcp/tools/registry.py)
to authorize BEFORE ``jsonschema.validate`` for any tool whose
implementation carries the ``_required_capability`` / ``_required_policy_keys``
attribute the ``@requires_capability`` / ``@requires_policy`` decorators
stamp. That fix explicitly did NOT cover tools that authorize via a plain
in-body helper call instead of a decorator — those set no such attribute,
so they fell through to schema validation first, leaking the tool's exact
required-field / type / pattern / additionalProperties shape to an
unauthorized caller via a malformed call.

Round 21's authz/IDOR lane found 12 such sites:

* agent_mcp/tools/admin_tools.py: register_agent, view_status,
  terminate_agent, restore_agent, edit_agent, purge_agent, view_audit_log,
  get_agent_tokens (all gated via the in-body ``_require_capability``
  helper).
* agent_mcp/tools/agent_communication_tools.py: broadcast_admin_message
  (gated via ``_is_operator_tier`` — a predicate, not a single capability:
  capability OR the legacy ``agent_id == "admin"`` label).
* agent_mcp/tools/project_context_tools.py: backup_project_context (gated
  via ``_is_admin_principal``, which reduces to a single capability check).
* agent_mcp/tools/project_settings_tools.py: view_project_settings,
  update_project_settings, delete_project_settings (gated via
  ``_deny_without_config_write_cap``, also a single capability check).

Fix: every in-body gate above is now expressed via ``@requires_capability``
(the 11 sites that reduce to a single capability string) or the new
``@requires_predicate`` decorator (``broadcast_admin_message``, whose
``_is_operator_tier`` predicate is capability-OR-legacy-label and can't be
expressed as a single capability without weakening it) — see
``agent_mcp/core/authorize.py``. Decorating stamps
``_required_capability`` / ``_required_predicate`` on the wrapper, which
``dispatch_tool_call``'s EXISTING R20-F4 pre-schema gate already reads —
no new dispatcher mechanism, just closing the "no decorator ⇒ no stamp ⇒
no pre-schema gate" gap for these 12 sites. The authorization DECISION
(which capability / predicate a caller needs) is unchanged; only WHEN it
runs moves earlier.

RED (pre-fix): every ``test_malformed_call_*`` case below raised
``ToolInputValidationError`` (schema shape leaked) instead of
``AuthRejected``.
GREEN (post-fix): the same calls raise ``AuthRejected`` with the exact
message a well-formed call from the same unauthorized caller gets.
"""

from __future__ import annotations

import pytest

from tests.harness import make_principal

pytestmark = pytest.mark.asyncio


def _worker_principal():
    """An agent-bearer worker: carries NONE of the caps / predicates
    these 12 tools require (agents.register, agents.terminate,
    system.config.write are all operator-only; the worker's agent_id
    isn't the legacy "admin" label ``_is_operator_tier`` also admits)."""
    return make_principal(kind="agent_bearer", agent_id="worker_r21f1", agent_role="worker")


def _operator_principal():
    """An operator-session Principal: carries every cap the 12 tools
    below require (PROJECT_ROLE_BUNDLES["operator"])."""
    return make_principal(
        kind="operator_session",
        user_id="op-r21f1",
        project_name="proj",
        project_role="operator",
    )


# Each entry: (tool_name, malformed_args, wellformed_args, forbidden_marker)
#
# ``malformed_args`` fails jsonschema.validate for the tool's registered
# inputSchema. ``forbidden_marker`` is the jsonschema wording that
# revealed the shape pre-fix (required-field name, enum values, pattern
# text, or the additionalProperties phrasing) — it must NEVER appear in
# the AuthRejected message an unauthorized caller gets, malformed or not.
CASES = [
    (
        "register_agent",
        {"role": "not-a-real-role"},
        {"name": "wf-test-agent", "role": "worker"},
        "is not one of",
    ),
    (
        "view_status",
        {"bogus_field_r21f1": 1},
        {},
        "Additional properties",
    ),
    (
        "terminate_agent",
        {},
        {"agent_id": "some-agent"},
        "is a required property",
    ),
    (
        "restore_agent",
        {},
        {"agent_id": "some-agent"},
        "is a required property",
    ),
    (
        "edit_agent",
        {},
        {"agent_id": "some-agent"},
        "is a required property",
    ),
    (
        "purge_agent",
        {},
        {"agent_id": "some-agent"},
        "is a required property",
    ),
    (
        "view_audit_log",
        {"limit": "not-an-int"},
        {},
        "is not of type",
    ),
    (
        "get_agent_tokens",
        {"bogus_field_r21f1": 1},
        {},
        "Additional properties",
    ),
    (
        "broadcast_admin_message",
        {},
        {"message": "hello"},
        "is a required property",
    ),
    (
        "backup_project_context",
        {"backup_name": "../../etc/passwd"},
        {},
        "does not match",
    ),
    (
        "view_project_settings",
        {"bogus_field_r21f1": 1},
        {},
        "Additional properties",
    ),
    (
        "update_project_settings",
        {},
        {"context_key": "config_r21f1_test", "context_value": True},
        "is a required property",
    ),
    (
        "delete_project_settings",
        {},
        {"context_key": "config_r21f1_test"},
        "is a required property",
    ),
]


@pytest.mark.parametrize(
    "tool_name,malformed_args,wellformed_args,forbidden_marker", CASES,
    ids=[c[0] for c in CASES],
)
async def test_malformed_call_by_unauthorized_worker_is_denied_not_schema_leaked(
    tool_name, malformed_args, wellformed_args, forbidden_marker,
) -> None:
    """RED: an unauthorized worker's MALFORMED call must be denied
    BEFORE schema validation — no schema-shape text may reach it."""
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.registry import dispatch_tool_call

    with pytest.raises(AuthRejected) as excinfo:
        await dispatch_tool_call(
            tool_name, malformed_args, principal=_worker_principal(),
        )

    message = str(excinfo.value)
    assert forbidden_marker not in message, (
        f"{tool_name}: AuthRejected message leaked schema shape: {message!r}"
    )
    assert "Unauthorized" in message


@pytest.mark.parametrize(
    "tool_name,malformed_args,wellformed_args,forbidden_marker", CASES,
    ids=[c[0] for c in CASES],
)
async def test_wellformed_call_by_unauthorized_worker_gets_same_denial(
    tool_name, malformed_args, wellformed_args, forbidden_marker,
) -> None:
    """Regression (a): a WELL-FORMED unauthorized call must get the
    IDENTICAL denial the malformed call gets — no message drift between
    the two paths, and no change in the correct pre-existing behavior
    for well-formed calls."""
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.registry import dispatch_tool_call

    with pytest.raises(AuthRejected) as excinfo_malformed:
        await dispatch_tool_call(
            tool_name, malformed_args, principal=_worker_principal(),
        )
    with pytest.raises(AuthRejected) as excinfo_wellformed:
        await dispatch_tool_call(
            tool_name, wellformed_args, principal=_worker_principal(),
        )

    assert str(excinfo_malformed.value) == str(excinfo_wellformed.value), (
        f"{tool_name}: malformed vs well-formed denial text drifted"
    )


@pytest.mark.parametrize(
    "tool_name,malformed_args,wellformed_args,forbidden_marker", CASES,
    ids=[c[0] for c in CASES],
)
async def test_malformed_call_by_authorized_caller_still_schema_validates(
    tool_name, malformed_args, wellformed_args, forbidden_marker,
) -> None:
    """Regression (b): an AUTHORIZED caller's malformed body must still
    get the normal jsonschema validation error — this fix only changes
    behavior for UNAUTHORIZED callers."""
    from agent_mcp.tools.registry import ToolInputValidationError, dispatch_tool_call

    operator = _operator_principal()

    with pytest.raises(ToolInputValidationError) as excinfo:
        await dispatch_tool_call(tool_name, malformed_args, principal=operator)

    assert forbidden_marker in str(excinfo.value), (
        f"{tool_name}: authorized caller must still see the real schema "
        f"validation error, got: {excinfo.value}"
    )


async def test_nonexistent_tool_still_reports_not_found_for_any_role() -> None:
    """Regression (c): a nonexistent tool name must still resolve to
    NotFound regardless of the caller's role — the new gates on these 12
    sites must not accidentally leak auth info for unknown tool names."""
    from agent_mcp.core.tool_result import NotFound
    from agent_mcp.tools.registry import dispatch_tool_call

    result = await dispatch_tool_call(
        "this_tool_does_not_exist_r21f1", {}, principal=_worker_principal(),
    )
    assert isinstance(result, NotFound)


# --- broadcast_admin_message's specific predicate: legacy "admin" label ---


async def test_broadcast_admin_message_legacy_admin_label_still_admits() -> None:
    """``@requires_predicate`` on ``broadcast_admin_message`` wraps the
    SAME ``_is_operator_tier`` predicate the in-body check used —
    including the legacy ``agent_id == "admin"`` harness escape hatch.
    Converting the mechanism must not narrow this to a pure capability
    check (that would be a policy change, not a mechanism fix)."""
    from agent_mcp.core.tool_result import PermissionDenied
    from agent_mcp.tools.registry import dispatch_tool_call

    admin_label_principal = make_principal(
        kind="agent_bearer", agent_id="admin", agent_role="manager",
    )
    result = await dispatch_tool_call(
        "broadcast_admin_message",
        {"message": "hello from legacy admin label"},
        principal=admin_label_principal,
    )
    # Admits past the gate — whatever it returns is not a denial.
    assert not isinstance(result, PermissionDenied)


# --- class-sweep: every one of the 12 sites is now stamped -------------


@pytest.mark.parametrize("tool_name,_a,_b,_c", CASES, ids=[c[0] for c in CASES])
async def test_tool_implementation_is_stamped_for_pre_schema_gate(
    tool_name, _a, _b, _c,
) -> None:
    """Every one of the 12 R21-F1 sites now carries EITHER
    ``_required_capability`` or ``_required_predicate`` on its
    registered implementation — the attribute ``dispatch_tool_call``'s
    R20-F4 pre-schema gate reads. This is the actual mechanism the fix
    relies on; the behavioral tests above prove its effect."""
    from agent_mcp.tools.registry import tool_implementations

    impl = tool_implementations[tool_name]
    has_cap = getattr(impl, "_required_capability", None) is not None
    has_predicate = getattr(impl, "_required_predicate", None) is not None
    assert has_cap or has_predicate, (
        f"{tool_name}: implementation carries neither "
        f"_required_capability nor _required_predicate — the R20-F4 "
        f"pre-schema gate in dispatch_tool_call can't see it"
    )
