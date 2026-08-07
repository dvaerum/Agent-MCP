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
from typing import Any

import pytest

from tests.harness import mcp_session, with_bearer

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
        # token-retirement PR 1: the validator no longer takes an
        # ``auth_token`` arg; it takes the caller's Principal (forwarded
        # only to the ImportError-fallback RAG call). Accept it so this
        # stand-in keeps the same signature as the patched target.
        principal: Any = None,
        # R5-F1: the real validator now threads the caller's RAG scope
        # (requesting_agent_id + can_view_all_tasks). Accept them so this
        # stand-in keeps the same signature as the patched target.
        requesting_agent_id: str | None = None,
        can_view_all_tasks: bool = True,
    ) -> dict[str, Any]:
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


# --- Defense-in-depth: `is True` rejects non-True truthy values ------------
#
# INFO-001 (audit-A, 2026-06-30 follow-up): real MCP clients are
# protected by the registry's jsonschema check (``"type": "boolean"``),
# which coerces ``"true"``/``"false"``/``1``/``0`` to a bool before the
# impl runs — so ``bool()`` at the impl seam is defensively equivalent
# for wire callers.
#
# The gap is in-process callers that skip the dispatcher (custom
# scripts, migrations, integration harnesses that reach around the
# registry). ``bool("false") is True`` in Python: a legacy consumer
# passing a stringified ``"false"`` would silently opt in. The fix
# uses ``arguments.get("accept_suggestions", False) is True`` — the
# comparison admits only the actual singleton, so string/int/None/
# any truthy object all coerce to False (i.e. no consent).
#
# These tests hit the impl directly (bypassing the schema check) with
# the "true"/1 shapes an in-process caller might pass and assert the
# mutation is NOT applied.


async def test_assign_task_impl_rejects_string_accept_suggestions(
    tmp_path, monkeypatch,
) -> None:
    """Calling the impl directly with ``accept_suggestions="true"``
    (string, not bool) must NOT apply validator suggestions —
    ``is True`` correctly rejects the string.

    Real MCP clients hit the jsonschema gate first so they never see
    this path, but in-process callers can bypass the dispatcher; this
    pins the defense-in-depth behaviour at the impl seam.
    """
    from agent_mcp.tools.task_tools import assign_task_tool_impl

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
        victim_title = "victim task with string 'true' consent"
        with with_bearer(admin.admin_token):
            result = await assign_task_tool_impl(
                {
                    "token": admin.admin_token,
                    "agent_token": bob.token,
                    "task_title": victim_title,
                    "task_description": "in-process caller passed string 'true'",
                    "parent_task_id": legit_parent_id,
                    # A string — NOT the True singleton. ``bool("true")``
                    # is True, but the impl uses ``is True`` so the string
                    # is rejected and no consent is inferred.
                    "accept_suggestions": "true",
                },
            )

        # Result must NOT be Ok (that would mean the mutation happened);
        # extract text from whichever typed variant we got.
        text = getattr(result, "message", None) or (
            result[0].text if isinstance(result, list) else str(result)
        )

        # Response should surface the opt-in hint (same as default
        # unopted path) — because the string is treated as no consent.
        assert "accept_suggestions=true" in text, (
            f"impl should treat string 'true' as no-consent; got: {text}"
        )

        # And no task must have been persisted with the attacker's
        # suggested parent.
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
            "in-process caller passing accept_suggestions='true' "
            "(string) must NOT trigger the mutation — the impl's "
            "`is True` guard is what enforces this; "
            f"found {n_victim} rows"
        )
        assert n_under_attacker == 0, (
            f"no task should have been silently re-parented under the "
            f"attacker's target ({attacker_target_id}) via string "
            f"consent; found {n_under_attacker} rows"
        )


async def test_create_self_task_impl_rejects_int_accept_suggestions(
    tmp_path, monkeypatch,
) -> None:
    """Same guarantee on the agent self-task path: integer ``1``
    (or any non-True truthy value) must NOT be treated as consent.
    """
    from agent_mcp.tools.task_tools import create_self_task_tool_impl

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

        victim_title = "alice self-task with int 1 consent"
        with with_bearer(alice.token):
            result = await create_self_task_tool_impl(
                {
                    "token": alice.token,
                    "task_title": victim_title,
                    "task_description": "in-process caller passed int 1",
                    "parent_task_id": legit_parent_id,
                    # Integer ``1`` is truthy under ``bool()`` but is
                    # not the True singleton — ``is True`` rejects it.
                    "accept_suggestions": 1,
                },
            )

        text = getattr(result, "message", None) or (
            result[0].text if isinstance(result, list) else str(result)
        )
        assert "accept_suggestions=true" in text, (
            f"impl should treat int 1 as no-consent; got: {text}"
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
            "in-process caller passing accept_suggestions=1 (int) "
            "must NOT trigger the mutation; "
            f"found {n_victim} rows"
        )
        assert n_under_attacker == 0, (
            f"self-task path must NOT silently re-parent under "
            f"attacker choice ({attacker_target_id}) via int consent; "
            f"found {n_under_attacker} rows"
        )
