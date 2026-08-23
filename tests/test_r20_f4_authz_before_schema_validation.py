"""R20-F4: dispatch_tool_call must authorize BEFORE schema-validating.

Before this fix, ``dispatch_tool_call`` (agent_mcp/tools/registry.py)
ran jsonschema validation on the caller's raw arguments BEFORE
resolving/checking the capability or policy gate stamped on the tool
by ``@requires_capability`` / ``@requires_policy``. ``tools/list``
already hides operator-only tools from a worker bearer
(``tools/access.py``), but ``dispatch_tool_call`` had no equivalent
pre-dispatch filter — so a worker sending a MALFORMED call to a
hidden, operator-only tool got back a jsonschema
``ToolInputValidationError`` naming the tool's exact required-field /
type / ``additionalProperties`` shape, BEFORE the authorization gate
ever ran. Only a WELL-FORMED call reached the (correct) capability
denial. That let a caller distinguish "tool exists but I'm
unauthorized" / "tool doesn't exist" / "tool exists, here's its exact
schema" entirely pre-auth — a tool-discovery oracle (LOW: information
disclosure, not a privilege escalation; the well-formed call was
always correctly denied).

Fix: the dispatcher now consults the SAME ``_required_capability`` /
``_required_policy_keys`` signal ``tools/access.py`` already reads off
the tool's wrapper (stamped by the decorators in
``agent_mcp.core.authorize``) and evaluates the gate BEFORE running
``jsonschema.validate`` — reusing the decorators' own gate-checking
helpers (``check_capability_gate`` / ``check_policy_gate``) so the
two evaluations of "does this principal pass" can never diverge.

RED (pre-fix): the first test below raised ``ToolInputValidationError``
(schema shape leaked) instead of ``AuthRejected``.
GREEN (post-fix): the same call raises ``AuthRejected`` with the exact
message a well-formed call would have gotten.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from tests.harness import make_principal

pytestmark = pytest.mark.asyncio


def _worker_principal():
    return make_principal(kind="agent_bearer", agent_id="worker_a", agent_role="worker")


def _operator_principal():
    return make_principal(
        kind="operator_session",
        user_id="alice",
        project_name="proj",
        project_role="operator",
    )


# --- capability-gated tool (delete_task, @requires_capability("tasks.delete")) ---


async def test_malformed_call_by_unauthorized_worker_is_denied_not_schema_leaked():
    """RED: worker lacks ``tasks.delete``; body is missing the required
    ``task_id``. The denial must fire BEFORE schema validation — no
    schema-shape text (``task_id``) may reach an unauthorized caller.
    """
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.registry import dispatch_tool_call

    with pytest.raises(AuthRejected) as excinfo:
        await dispatch_tool_call("delete_task", {}, principal=_worker_principal())

    message = str(excinfo.value)
    assert "tasks.delete" in message
    assert "task_id" not in message


async def test_wellformed_call_by_unauthorized_worker_gets_same_denial(
) -> None:
    """Regression (a): a WELL-FORMED unauthorized call must get the
    identical capability-specific denial the pre-fix code already gave
    — no change in the correct, existing behavior for well-formed
    calls.
    """
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.registry import dispatch_tool_call

    with pytest.raises(AuthRejected) as excinfo:
        await dispatch_tool_call(
            "delete_task",
            {"task_id": "task_1"},
            principal=_worker_principal(),
        )

    assert "tasks.delete" in str(excinfo.value)


async def test_malformed_call_by_authorized_caller_still_schema_validates() -> None:
    """Regression (b): an AUTHORIZED caller's malformed body must still
    get the normal jsonschema validation error — this fix only changes
    behavior for UNAUTHORIZED callers.
    """
    from agent_mcp.tools.registry import ToolInputValidationError, dispatch_tool_call

    operator = _operator_principal()
    assert operator.has_capability("tasks.delete")

    with pytest.raises(ToolInputValidationError) as excinfo:
        await dispatch_tool_call("delete_task", {}, principal=operator)

    assert "task_id" in str(excinfo.value)


async def test_nonexistent_tool_still_reports_not_found_for_any_role() -> None:
    """Regression (c): a nonexistent tool name must still resolve to
    NotFound regardless of the caller's role — the new pre-dispatch
    gate must not accidentally leak auth info for unknown tool names.
    """
    from agent_mcp.core.tool_result import NotFound
    from agent_mcp.tools.registry import dispatch_tool_call

    result = await dispatch_tool_call(
        "this_tool_does_not_exist_r20f4",
        {},
        principal=_worker_principal(),
    )
    assert isinstance(result, NotFound)


# --- policy-gated tool (@requires_policy) — same ordering bug class ---


async def test_malformed_call_to_policy_gated_tool_by_disallowed_worker_is_denied(
    reset_globals,
) -> None:
    """The same oracle exists for ``@requires_policy``-gated tools: a
    worker whom the toggle does not admit must be denied BEFORE schema
    validation, not after a jsonschema shape leak.
    """
    from agent_mcp.core.authorize import AuthRejected, requires_policy
    from agent_mcp.core.tool_result import Ok, ToolResult
    from agent_mcp.tools.registry import (
        ToolInputValidationError,
        dispatch_tool_call,
        register_tool,
    )

    toggle_key = "config_r20f4_test_toggle_never_set"

    @requires_policy(toggle_key, default=False)
    async def _r20f4_stub_impl(
        arguments: Dict[str, Any], *, principal: Optional[Any] = None
    ) -> ToolResult:  # pragma: no cover - never reached when denied
        return Ok(message="ran")

    register_tool(
        name="_r20f4_test_policy_tool",
        description="R20-F4 regression stub",
        input_schema={
            "type": "object",
            "properties": {"required_field": {"type": "string"}},
            "required": ["required_field"],
            "additionalProperties": False,
        },
        implementation=_r20f4_stub_impl,
        visibility=f"worker-if-toggled:{toggle_key}",
    )

    with pytest.raises(AuthRejected) as excinfo:
        await dispatch_tool_call(
            "_r20f4_test_policy_tool", {}, principal=_worker_principal()
        )

    message = str(excinfo.value)
    assert toggle_key in message
    assert "required_field" not in message

    # A well-formed call from the same disallowed worker must get the
    # identical denial (no message drift between the malformed and
    # well-formed paths).
    with pytest.raises(AuthRejected) as excinfo2:
        await dispatch_tool_call(
            "_r20f4_test_policy_tool",
            {"required_field": "x"},
            principal=_worker_principal(),
        )
    assert str(excinfo2.value) == message

    # An operator-tier caller bypasses the toggle entirely and a
    # malformed body from THAT caller still schema-validates normally.
    with pytest.raises(ToolInputValidationError):
        await dispatch_tool_call(
            "_r20f4_test_policy_tool", {}, principal=_operator_principal()
        )
