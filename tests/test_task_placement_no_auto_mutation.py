"""VULN-004: task-placement validator must NEVER silently mutate
parent_task / dependencies.

Background
----------
``assign_task`` and ``create_self_task`` route every task through the
``validate_task_placement`` RAG pre-check. The validator's RAG corpus
includes ``project_context`` entries any agent can write — so a
poisoned context entry can later jailbreak the validator and have it
return attacker-chosen suggestions for ``parent_task`` and
``dependencies``.

Before this fix, the tool code unconditionally applied those
suggestions on the next task-creation call. That meant a worker could
plant a malicious context entry, then wait for an admin (or another
agent) to create a task — the admin's new task would get re-parented
under the attacker's chosen task, with no visible warning beyond
"RAG suggestions automatically applied".

The fix is an explicit ``accept_suggestions`` opt-in (default false).
When ``accept_suggestions=false`` (the default) and the validator
returns non-approved suggestions, the tool returns an Invalid
ToolResult containing the suggestions formatted as text — the caller
must explicitly opt in (and re-submit) before any mutation happens.

The tests below pin both halves of the new contract for both tools:

* default (no ``accept_suggestions``) → no mutation, surface as text;
* ``accept_suggestions=true`` → mutation as before, with the same
  ``✓ Applied suggested …`` lines in the response message.
"""

from __future__ import annotations

import re
from typing import Any, Dict

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# --- Helpers ---------------------------------------------------------------


def _make_validator_mock(
    suggested_parent: str | None,
    suggested_deps: list[str] | None = None,
    status: str = "suggest_changes",
) -> Any:
    """Build an async stand-in for ``validate_task_placement`` that
    always returns the given attacker-chosen suggestions.

    Returns a coroutine function with the same signature as the real
    validator. Tests monkey-patch this onto
    ``agent_mcp.tools.task_tools.validate_task_placement`` to simulate
    a RAG corpus that has been jailbroken via a poisoned
    project_context entry.

    NB: the suggested ``parent_task`` and ``dependencies`` must point
    at real task_ids — the ``tasks.parent_task`` FK declared by
    migration 0007 + 0008 rejects rows that name a missing parent.
    Tests seed the "attacker target" task explicitly so this constraint
    is satisfied; the security argument is unchanged because the
    attack scenario in VULN-004 also requires the attacker's target
    task to exist in the project before the validator can name it.
    """
    async def _fake_validate(
        title: str,
        description: str,
        parent_task_id: str | None,
        depends_on_tasks: list[str] | None,
        created_by: str,
        auth_token: str,
    ) -> Dict[str, Any]:
        return {
            "status": status,
            "suggestions": {
                "parent_task": suggested_parent,
                "dependencies": list(suggested_deps or []),
                "reasoning": "(poisoned validator output)",
            },
            "duplicates": [],
            "message": "Validator suggests attacker-chosen placement.",
        }

    return _fake_validate


# --- assign_task: admin path ----------------------------------------------


async def _seed_two_tasks(admin, alice_token: str):
    """Seed (legit_parent, attacker_target) as real rows: a root task
    and an attacker-controlled sibling whose task_id the validator will
    later try to reroute the victim under. Returns (legit_parent_id,
    attacker_target_id)."""
    root_result = await admin.assert_tool_succeeds(
        "assign_task",
        {
            "agent_token": alice_token,
            "task_title": "the only legitimate root",
            "task_description": "established before the attack",
        },
    )
    legit_parent_id = re.search(
        r"task_[a-f0-9]+", root_result[0].text
    ).group(0)

    atk_result = await admin.assert_tool_succeeds(
        "assign_task",
        {
            "agent_token": alice_token,
            "task_title": "attacker-controlled sibling",
            "task_description": (
                "the task the poisoned validator will name as "
                "the suggested parent"
            ),
            "parent_task_id": legit_parent_id,
        },
    )
    attacker_target_id = re.search(
        r"task_[a-f0-9]+", atk_result[0].text
    ).group(0)
    return legit_parent_id, attacker_target_id


# --- assign_task: admin path ----------------------------------------------


async def test_assign_task_default_does_not_apply_validator_suggestions(
    tmp_path, monkeypatch,
) -> None:
    """Default behavior (no ``accept_suggestions``): the validator's
    suggested parent_task / dependencies are NOT silently applied.

    The response surfaces the suggestions as text so the caller can
    evaluate them — but no task with the attacker's parent_task gets
    written.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        legit_parent_id, attacker_target_id = await _seed_two_tasks(
            admin, alice.token
        )

        # Jam the validator with attacker-chosen suggestions. Suggestion
        # points at the seeded attacker_target_id so the FK would be
        # satisfied if the suggestion WERE auto-applied — failure to
        # block must not be hidden behind a generic FK error.
        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_validator_mock(
                suggested_parent=attacker_target_id,
                suggested_deps=[],
                status="suggest_changes",
            ),
        )

        bob = await admin.create_worker("bob")
        victim_title = "victim task admin wants under legitimate parent"
        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_title": victim_title,
                "task_description": "admin's intent: place under legit parent",
                "parent_task_id": legit_parent_id,
                # NB: no `accept_suggestions` → default False.
            },
        )
        text = result[0].text

        # 1. Response must surface the suggestions to the caller.
        assert "accept_suggestions=true" in text, (
            f"response should hint at the opt-in arg; got: {text}"
        )
        assert attacker_target_id in text, (
            f"response should list the suggested (attacker) parent so "
            f"the caller can evaluate; got: {text}"
        )

        # 2. No NEW task should be persisted at all (Invalid response,
        #    not Ok). Only the two we seeded should be present.
        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE title = ?",
                (victim_title,),
            )
            n_victim = cur.fetchone()["n"]
            cur.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE parent_task = ?",
                (attacker_target_id,),
            )
            n_under_attacker = cur.fetchone()["n"]
        finally:
            conn.close()
        assert n_victim == 0, (
            "no task should have been persisted when validator returned "
            "suggestions and accept_suggestions defaulted to false; "
            f"found {n_victim} rows"
        )
        assert n_under_attacker == 0, (
            f"no task should have been silently re-parented under the "
            f"attacker's target ({attacker_target_id}); "
            f"found {n_under_attacker} rows"
        )


async def test_assign_task_with_accept_suggestions_applies_them(
    tmp_path, monkeypatch,
) -> None:
    """When the caller explicitly opts in with ``accept_suggestions=true``,
    the validator's suggested parent_task / dependencies ARE applied
    (preserves the legacy auto-apply behavior, gated on consent)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        legit_parent_id, attacker_target_id = await _seed_two_tasks(
            admin, alice.token
        )

        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_validator_mock(
                suggested_parent=attacker_target_id,
                suggested_deps=[attacker_target_id],
                status="suggest_changes",
            ),
        )

        bob = await admin.create_worker("bob")
        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_title": "explicit-opt-in task",
                "task_description": "caller knows what they're doing",
                "parent_task_id": legit_parent_id,
                "accept_suggestions": True,
            },
        )
        text = result[0].text
        # Must have succeeded with the auto-apply confirmation lines.
        assert "Applied suggested parent" in text, (
            f"expected validation_message to confirm suggestion apply; "
            f"got: {text}"
        )
        assert attacker_target_id in text, (
            f"expected applied parent to be the (mock) suggestion; "
            f"got: {text}"
        )

        # The persisted task must carry the suggested parent. Extract
        # the assigned-task id specifically from the success line so we
        # don't confuse it with the seeded ids that also appear in text.
        m = re.search(r"Task ID:\s+(task_[a-f0-9]+)", text)
        assert m, f"no task_id in result text: {text}"
        new_task_id = m.group(1)

        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT parent_task, depends_on_tasks FROM tasks "
                "WHERE task_id = ?",
                (new_task_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, f"task {new_task_id} not persisted"
        assert row["parent_task"] == attacker_target_id, (
            f"expected parent_task={attacker_target_id}, got "
            f"{row['parent_task']!r}"
        )
        import json as _json
        deps = _json.loads(row["depends_on_tasks"] or "[]")
        assert attacker_target_id in deps, (
            f"expected suggested dependency to be applied; got {deps}"
        )


# --- create_self_task: agent path -----------------------------------------


async def test_create_self_task_default_does_not_apply_validator_suggestions(
    tmp_path, monkeypatch,
) -> None:
    """Same contract on the agent self-task path: default behavior must
    NOT mutate parent_task / dependencies based on validator output."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        legit_parent_id, attacker_target_id = await _seed_two_tasks(
            admin, alice.token
        )

        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_validator_mock(
                suggested_parent=attacker_target_id,
                suggested_deps=[],
                status="suggest_changes",
            ),
        )

        victim_title = "alice self-task victim"
        result = await alice.call(
            "create_self_task",
            {
                "task_title": victim_title,
                "task_description": "alice's own request",
                "parent_task_id": legit_parent_id,
                # No accept_suggestions → default False.
            },
        )
        text = result[0].text
        assert "accept_suggestions=true" in text, (
            f"agent path should also surface the opt-in hint; got: {text}"
        )
        assert attacker_target_id in text, (
            f"agent path should surface the suggested parent for review; "
            f"got: {text}"
        )

        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE title = ?",
                (victim_title,),
            )
            n_victim = cur.fetchone()["n"]
            cur.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE parent_task = ?",
                (attacker_target_id,),
            )
            n_under_attacker = cur.fetchone()["n"]
        finally:
            conn.close()
        assert n_victim == 0, (
            "victim self-task must not have been persisted when "
            "accept_suggestions defaulted to false"
        )
        assert n_under_attacker == 0, (
            f"create_self_task must NOT silently re-parent under "
            f"attacker choice ({attacker_target_id}); "
            f"found {n_under_attacker} rows"
        )


async def test_create_self_task_with_accept_suggestions_applies_them(
    tmp_path, monkeypatch,
) -> None:
    """Agent path: ``accept_suggestions=true`` applies the validator's
    suggestions (preserves legacy behavior, gated on consent)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        legit_parent_id, attacker_target_id = await _seed_two_tasks(
            admin, alice.token
        )

        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_validator_mock(
                suggested_parent=attacker_target_id,
                suggested_deps=[attacker_target_id],
                status="suggest_changes",
            ),
        )

        result = await alice.call(
            "create_self_task",
            {
                "task_title": "alice opt-in self-task",
                "task_description": "alice explicitly accepts suggestions",
                "parent_task_id": legit_parent_id,
                "accept_suggestions": True,
            },
        )
        text = result[0].text
        assert "Applied suggested parent" in text, (
            f"expected validation_message to confirm suggestion apply; "
            f"got: {text}"
        )

        # Pull the new task and verify the parent / deps. The
        # "Self-assigned task '<id>' created." line carries the new id
        # explicitly so we don't pick up the seeded ids that also
        # appear in the text.
        m = re.search(
            r"Self-assigned task '(task_[a-f0-9]+)' created", text
        )
        assert m, f"could not extract new task_id from: {text}"
        new_task_id = m.group(1)

        from agent_mcp.db.connection import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT parent_task, depends_on_tasks FROM tasks "
                "WHERE task_id = ?",
                (new_task_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, f"task {new_task_id} not persisted"
        assert row["parent_task"] == attacker_target_id, (
            f"with accept_suggestions=true, parent_task should be the "
            f"suggested value; got {row['parent_task']!r}"
        )
        import json as _json
        deps = _json.loads(row["depends_on_tasks"] or "[]")
        assert attacker_target_id in deps, (
            f"expected suggested dependency to be applied; got {deps}"
        )
