"""Unit coverage of :func:`agent_mcp.core.tool_result.tool_result_error_message`.

arch-r4 #10 (arch-deepening round 4): this is the ONE variant→string
mapper that used to be re-implemented three times as private helpers
on the REST routers (``_agent_tool_error`` in ``app/routers/agents.py``,
``_tool_error_detail`` in ``app/routers/tasks.py``,
``_memory_create_error_detail`` in ``app/routers/memories.py``). These
tests pin the exact per-variant mapping AND the three routers' historical
NotFound wordings, which the ``not_found_label`` override reproduces.
"""

from __future__ import annotations

from agent_mcp.core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    tool_result_error_message,
)


def test_not_found_defaults_to_resource_label() -> None:
    """No ``not_found_label`` override → use the ToolResult's own
    ``resource`` field, verbatim. This is ``memories.py``'s historical
    behavior (``_memory_create_error_detail``)."""
    result = NotFound(resource="context_key", identifier="secret.token")
    assert (
        tool_result_error_message(result, "fallback")
        == "context_key 'secret.token' not found"
    )


def test_not_found_label_override_replaces_resource() -> None:
    """A caller-supplied ``not_found_label`` wins over ``result.resource``
    — this is how ``agents.py`` (``"Agent"``, capitalized, ignoring the
    tool's ``resource="agent"``) and ``tasks.py`` (``"Parent task"``)
    preserve their pre-consolidation wording."""
    result = NotFound(resource="agent", identifier="worker-1")
    assert (
        tool_result_error_message(result, "fallback", not_found_label="Agent")
        == "Agent 'worker-1' not found"
    )

    parent_result = NotFound(resource="task", identifier="task_abc123")
    assert (
        tool_result_error_message(
            parent_result, "fallback", not_found_label="Parent task",
        )
        == "Parent task 'task_abc123' not found"
    )


def test_conflict_uses_reason() -> None:
    result = Conflict(reason="Memory with this key already exists")
    assert (
        tool_result_error_message(result, "fallback")
        == "Memory with this key already exists"
    )


def test_permission_denied_uses_reason() -> None:
    result = PermissionDenied(reason="only the note's author may delete it")
    assert (
        tool_result_error_message(result, "fallback")
        == "only the note's author may delete it"
    )


def test_invalid_uses_message() -> None:
    result = Invalid(message="task_title must not be empty", field="task_title")
    assert (
        tool_result_error_message(result, "fallback")
        == "task_title must not be empty"
    )


def test_failed_uses_fallback_not_internal_message() -> None:
    """SEC-R8-1: ``Failed.message`` can embed DB internals (table/column
    names, bound params) — it must NEVER reach the client. The route's
    static ``fallback`` string is returned instead."""
    result = Failed(message="sqlite3.IntegrityError: UNIQUE constraint tasks.task_id")
    assert tool_result_error_message(result, "Failed to create task") == (
        "Failed to create task"
    )


def test_ok_falls_back_too() -> None:
    """``Ok`` isn't a documented input (callers only reach this mapper on
    a non-``Ok`` branch) but the ladder is defensive: any variant that
    isn't NotFound/Conflict/PermissionDenied/Invalid renders the
    fallback rather than raising."""
    assert tool_result_error_message(Ok(), "fallback") == "fallback"


def test_fallback_defaults_to_operation_failed() -> None:
    result = Failed(message="boom")
    assert tool_result_error_message(result) == "Operation failed"
