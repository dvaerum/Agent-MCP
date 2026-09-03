"""arch-r4 #9 (MEDIUM): strip the task-placement validator down to a
PURE ADVISORY transform and confirm the single-root-task invariant is
still enforced upstream.

Background
----------
The "only one root task" invariant is a HARD structural constraint,
already enforced with a ``COUNT(*) ... WHERE parent_task IS NULL`` DB
guard THREE times in ``agent_mcp/tools/task_tools.py`` — twice on the
admin ``assign_task`` path (once outside the transaction as an early
UX short-circuit with suggestions, once inside the transaction as the
authoritative check) and once on the agent ``create_self_task`` path.
All three run and return ``Conflict`` BEFORE ``validate_task_placement``
is ever called.

``agent_mcp/features/task_placement/validator.py`` used to
re-implement the same invariant SOFTLY: it opened its own
``get_db_connection()`` (a DB-layer leak across the ``features``
module boundary), counted roots, injected the count into an LLM
prompt, and then trusted the LLM's self-reported
``hierarchy_analysis.hierarchy_violation`` flag to override the
status to ``"denied"``. Because the upstream guards always reject a
parent-less second root first, that branch could never fire on a real
violation — pure redundant, unreachable scaffolding routing a
checkable constraint through an unverifiable LLM round-trip.

This module now does NO database access at all and NEVER denies a
task based on hierarchy. It only suggests a parent, suggests
dependency changes, and flags likely duplicate tasks. This file pins
that contract:

1. ``validate_task_placement`` is unit-testable with a canned RAG
   response and NO database — proving the DB access is gone.
2. The single-root invariant is still enforced (via ``Conflict``) by
   ``task_tools.py``'s hard guard, confirming the removal was safe.
"""

from __future__ import annotations

import inspect

import pytest

import agent_mcp.tools.task_tools  # noqa: F401  (see NB above)
from agent_mcp.features.task_placement import validator as validator_mod
from agent_mcp.features.task_placement.suggestions import (
    format_suggestions_for_agent,
)
from agent_mcp.features.task_placement.validator import validate_task_placement

# NB: import order matters here (pre-existing, unrelated to this PR's
# change). ``agent_mcp.tools.task_tools`` imports
# ``agent_mcp.features.task_placement.validator``, and validator.py in
# turn imports ``agent_mcp.tools.rag_tools`` — a genuine module-level
# cycle. Importing ``agent_mcp.tools.task_tools`` FIRST (which is what
# every real entry point into the app does, via ``agent_mcp.tools``'s
# package __init__) makes the cycle resolve cleanly; importing
# ``validator`` as the very first touch of either package trips
# "cannot import name ... from partially initialized module". Import
# ``tests.harness`` first (it pulls in the full app) to sidestep it.
from tests.harness import mcp_session

# NB: no module-level ``pytestmark = pytest.mark.asyncio`` — this file
# mixes sync structural tests with async ones, and applying the mark
# module-wide triggers a PytestWarning on every sync test. Each async
# test is marked individually instead.


# --- Structural: no DB access anywhere in the module -----------------------


def test_validator_module_has_no_db_access() -> None:
    """The ``features`` layer must never open its own DB connection —
    that's a DB-layer leak across the module boundary. Pin this
    structurally so a future edit can't silently reintroduce it."""
    source = inspect.getsource(validator_mod)
    assert "get_db_connection" not in source, (
        "validator.py must not import or call get_db_connection(); the "
        "single-root invariant is enforced in task_tools.py, not here"
    )
    assert "db.connection" not in source, (
        "validator.py must not import from the db.connection module at all"
    )


def test_validator_never_reports_hierarchy_analysis() -> None:
    """The hierarchy-violation override (and the field it read from)
    is gone — the returned dict must never carry a ``hierarchy_analysis``
    key, and the prompt-formatting helper must not reference it either."""
    source = inspect.getsource(validator_mod)
    assert "hierarchy_analysis" not in source
    assert "hierarchy_violation" not in source

    import agent_mcp.features.task_placement.suggestions as suggestions_mod

    suggestions_source = inspect.getsource(suggestions_mod)
    assert "hierarchy_analysis" not in suggestions_source
    assert "hierarchy_violation" not in suggestions_source


# --- Pure unit test: canned RAG response, no DB, no real LLM ---------------


def _canned_rag_response(
    recommended_parent: str | None = "task_abc123",
    add_deps: list[str] | None = None,
    remove_deps: list[str] | None = None,
    similar_tasks: list[dict] | None = None,
    overall_recommendation: str = "modify",
) -> str:
    import json as _json

    return _json.dumps(
        {
            "placement_assessment": "needs_adjustment",
            "parent_suggestion": {
                "recommended_parent": recommended_parent,
                "reasoning": "closest match by topic",
            },
            "dependency_suggestions": {
                "add_dependencies": add_deps or [],
                "remove_dependencies": remove_deps or [],
                "reasoning": "workflow ordering",
            },
            "duplication_check": {
                "similar_tasks": similar_tasks or [],
                "is_duplicate": bool(similar_tasks),
            },
            "critical_thinking_summary": "fits under the suggested parent",
            "overall_recommendation": overall_recommendation,
            "message": "Consider the suggested parent.",
        }
    )


@pytest.mark.asyncio
async def test_validate_task_placement_maps_parent_and_dependency_suggestions(
    monkeypatch,
) -> None:
    """Feed a canned RAG response (no DB, no real network/LLM call) and
    assert the parent-suggestion / dependency-suggestion mapping — the
    genuinely advisory work this validator keeps."""

    async def _fake_query_rag_system_with_model(
        *,
        query_text: str,
        max_tokens: int,
        requesting_agent_id: str | None = None,
        can_view_all_tasks: bool = True,
        include_foreign: bool = False,
    ) -> str:
        return _canned_rag_response(
            recommended_parent="task_parent001",
            add_deps=["task_dep001"],
            remove_deps=["task_old_dep"],
        )

    monkeypatch.setattr(
        "agent_mcp.features.rag.query.query_rag_system_with_model",
        _fake_query_rag_system_with_model,
    )

    result = await validate_task_placement(
        title="Implement widget",
        description="Build the new widget subsystem",
        parent_task_id="task_old_dep",  # proposed dep to be removed
        depends_on_tasks=["task_old_dep"],
        created_by="admin",
    )

    assert result["status"] == "suggest_changes"
    assert result["suggestions"]["parent_task"] == "task_parent001"
    assert "task_dep001" in result["suggestions"]["dependencies"]
    assert "task_old_dep" not in result["suggestions"]["dependencies"]
    assert result["duplicates"] == []
    assert "hierarchy_analysis" not in result


@pytest.mark.asyncio
async def test_validate_task_placement_maps_duplicate_detection(monkeypatch) -> None:
    """Duplicate-detection is the other genuinely advisory feature that
    must survive the strip-down — assert the mapping from the RAG
    response's ``duplication_check`` block into ``result["duplicates"]``."""

    async def _fake_query_rag_system_with_model(
        *,
        query_text: str,
        max_tokens: int,
        requesting_agent_id: str | None = None,
        can_view_all_tasks: bool = True,
        include_foreign: bool = False,
    ) -> str:
        return _canned_rag_response(
            recommended_parent=None,
            similar_tasks=[
                {
                    "task_id": "task_existing001",
                    "title": "Implement widget (duplicate)",
                    "similarity": 0.93,
                    "reasoning": "near-identical title and description",
                }
            ],
        )

    monkeypatch.setattr(
        "agent_mcp.features.rag.query.query_rag_system_with_model",
        _fake_query_rag_system_with_model,
    )

    result = await validate_task_placement(
        title="Implement widget",
        description="Build the new widget subsystem",
        parent_task_id="task_parent001",
        depends_on_tasks=None,
        created_by="admin",
    )

    assert result["duplicates"] == [
        {
            "task_id": "task_existing001",
            "similarity": 0.93,
            "title": "Implement widget (duplicate)",
        }
    ]


@pytest.mark.asyncio
async def test_validate_task_placement_root_proposal_never_denied_by_hierarchy(
    monkeypatch,
) -> None:
    """Even when the RAG response's ``overall_recommendation`` is
    ``"proceed"`` for a root-level proposal (``parent_task_id=None``),
    the validator must never deny it on hierarchy grounds — it has no
    DB access to check root-count against, and the invariant is
    enforced elsewhere. This pins the removal of the old
    ``if hierarchy_violation and parent_task_id is None: status =
    "denied"`` override."""

    async def _fake_query_rag_system_with_model(
        *,
        query_text: str,
        max_tokens: int,
        requesting_agent_id: str | None = None,
        can_view_all_tasks: bool = True,
        include_foreign: bool = False,
    ) -> str:
        return _canned_rag_response(
            recommended_parent=None,
            overall_recommendation="proceed",
        )

    monkeypatch.setattr(
        "agent_mcp.features.rag.query.query_rag_system_with_model",
        _fake_query_rag_system_with_model,
    )

    result = await validate_task_placement(
        title="Root-level proposal",
        description="Proposed as a root task",
        parent_task_id=None,
        depends_on_tasks=None,
        created_by="admin",
    )

    assert result["status"] == "approved"
    assert "hierarchy_analysis" not in result


def test_format_suggestions_for_agent_has_no_hierarchy_violation_text() -> None:
    """``format_suggestions_for_agent`` must not surface the dead
    "HIERARCHY VIOLATION" banner — the validator can no longer produce
    that signal at all."""
    validation_result = {
        "status": "suggest_changes",
        "suggestions": {
            "parent_task": "task_parent001",
            "dependencies": [],
            "reasoning": "closest match",
        },
        "duplicates": [],
        "message": "Consider the suggested parent.",
    }
    text = format_suggestions_for_agent(validation_result, None, [])
    assert "HIERARCHY VIOLATION" not in text


# --- Confirm the single-root invariant is STILL enforced upstream ----------


@pytest.mark.asyncio
async def test_second_root_task_still_rejected_by_task_tools_hard_guard(
    tmp_path,
) -> None:
    """With the validator's hierarchy check removed, the ONLY thing
    standing between a caller and a second root task is
    ``task_tools.py``'s hard DB guard. Prove it still fires: creating a
    second parent-less task via ``assign_task`` must return a Conflict,
    and no second root row may be persisted.

    This exercises the REAL validator (no monkeypatching) — the point
    is that ``validate_task_placement`` is never even reached on the
    second call, because the guard at the top of the single-task path
    returns before it.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        first = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "the one legitimate root",
                "task_description": "established first",
            },
        )
        assert not getattr(admin, "_last_is_error", False), (
            f"first root task creation should succeed: {first[0].text}"
        )

        second = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "an illegitimate second root",
                "task_description": "attempts to create a second root task",
            },
        )
        text = second[0].text
        assert getattr(admin, "_last_is_error", False), (
            f"second root task creation should be rejected; got: {text}"
        )
        assert "root" in text.lower(), (
            f"rejection reason should mention the root-task rule; got: {text}"
        )

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE parent_task IS NULL"
            )
            root_count = cur.fetchone()["n"]
        finally:
            conn.close()
        assert root_count == 1, (
            f"exactly one root task must exist after the rejected second "
            f"attempt; found {root_count}"
        )


@pytest.mark.asyncio
async def test_agent_self_task_second_root_still_rejected_by_hard_guard(
    tmp_path,
) -> None:
    """Same invariant on the agent ``create_self_task`` path (the
    third hard guard in ``task_tools.py``, at the
    ``actual_parent_task_id is None`` check for admin-equivalent
    callers)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        first = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "the one legitimate root",
                "task_description": "established first",
            },
        )
        assert not getattr(admin, "_last_is_error", False), (
            f"first root task creation should succeed: {first[0].text}"
        )

        # Agents can never create root tasks at all (a separate, even
        # earlier guard) — assert that guard's Conflict fires too, and
        # the root count is still exactly one afterwards.
        second = await alice.call(
            "create_self_task",
            {
                "task_title": "alice tries to create a second root",
                "task_description": "no parent_task_id supplied",
            },
        )
        text = second[0].text
        assert getattr(alice, "_last_is_error", False), (
            f"agent-created root task should be rejected; got: {text}"
        )

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE parent_task IS NULL"
            )
            root_count = cur.fetchone()["n"]
        finally:
            conn.close()
        assert root_count == 1, (
            f"exactly one root task must exist after the rejected agent "
            f"self-task attempt; found {root_count}"
        )
