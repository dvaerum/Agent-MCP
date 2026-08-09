"""R15 Sibling 1b: single-root TOCTOU across the RAG await.

On the enforcing paths (``assign_task`` Mode 1 and ``create_self_task``)
the ``COUNT(root)`` guard runs BEFORE the suspending
``await validate_task_placement(...)`` and — pre-fix — was NEVER
re-checked before the INSERT. Two concurrent root-creates therefore
both pass the pre-await check (0 roots each) and race to two roots.

The devs already re-check the assignability gate for exactly this
window immediately before the INSERT; the single-root check needs the
same treatment.

These tests reproduce the race deterministically: a stand-in for
``validate_task_placement`` commits a COMPETING root (on a *separate*
DB connection, exactly as a concurrent create would) during its
``await``, then returns ``approved``. The tool under test must observe
the competing root in its in-transaction re-check and REJECT its own
root-create — leaving exactly one root.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _insert_competing_root(title: str) -> None:
    """Commit a root task (parent_task NULL) on a fresh connection —
    mirrors a concurrent create landing during the RAG await."""
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.repositories import task_repo

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        task_repo.create(
            {
                "title": title,
                "description": "the competing root",
                "created_by": "admin",
                "status": "unassigned",
                "priority": "medium",
                "parent_task": None,
            },
            connection=cur,
        )
        conn.commit()
    finally:
        conn.close()


def _make_root_racing_validator(competing_title: str) -> Any:
    """Async stand-in for ``validate_task_placement`` that commits a
    competing root DURING its await, then approves so the tool proceeds
    toward its own (now-conflicting) root INSERT."""

    async def _fake_validate(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        _insert_competing_root(competing_title)
        return {
            "status": "approved",
            "suggestions": {
                "parent_task": None,
                "dependencies": [],
                "reasoning": "(approved)",
            },
            "duplicates": [],
            "message": "Placement approved.",
        }

    return _fake_validate


def _root_count() -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE parent_task IS NULL"
        )
        return cur.fetchone()["n"]
    finally:
        conn.close()


def _first_text(result: list[Any]) -> str:
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


async def test_assign_task_rejects_second_root_racing_rag_await(
    tmp_path, monkeypatch
) -> None:
    """assign_task Mode 1: a competing root committed during the RAG
    await must make this parentless assign_task reject — exactly one
    root survives."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")

        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_root_racing_validator("competing root A"),
        )

        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_title": "my racing root",
                "task_description": "should lose the root race",
                # No parent_task_id → root create.
            },
        )
        text = _first_text(result)
        assert getattr(admin, "_last_is_error", False) or text.startswith(
            "Error:"
        ), f"expected rejection of the second root, got: {text!r}"

        assert _root_count() == 1, (
            "two concurrent root-creates both won — the single-root "
            "TOCTOU window is still open"
        )


async def test_create_self_task_rejects_second_root_racing_rag_await(
    tmp_path, monkeypatch
) -> None:
    """create_self_task (admin author): a competing root committed during
    the RAG await must make this parentless self-task reject — exactly
    one root survives."""
    async with mcp_session(tmp_path) as admin:
        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.validate_task_placement",
            _make_root_racing_validator("competing root B"),
        )

        result = await admin.call(
            "create_self_task",
            {
                "task_title": "my racing self root",
                "task_description": "should lose the root race",
                # No parent_task_id → root create (admin is exempt from
                # the agents-cannot-create-roots block).
            },
        )
        text = _first_text(result)
        assert getattr(admin, "_last_is_error", False) or text.startswith(
            "Error:"
        ), f"expected rejection of the second root, got: {text!r}"

        assert _root_count() == 1, (
            "create_self_task let a second root win the race — the "
            "single-root TOCTOU window is still open"
        )
