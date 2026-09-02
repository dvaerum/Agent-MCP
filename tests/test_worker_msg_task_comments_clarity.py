"""Worker-facing clarity for edit/delete_task_comment rejections.

Bug. ``task_comments_tools._classify_db_error`` collapses BOTH the DB
layer's "not found" and its "owned by <author>" ownership failure into a
bare ``NotFound(resource="task comment")`` that renders as
``"task comment '<id>' not found"``. A worker that is looking at a
comment plainly present in ``view_tasks`` — but authored by someone
else — is told the comment *doesn't exist*, reads that as a bug, and
files a false report. The message is honest about neither outcome.

Fix (worker-facing wording only). Fuse the two outcomes into ONE opaque
message that ADDS the author-only policy hint::

    task comment '<id>' not found, or you are not its author. Only a
    comment's original author (or an admin) can edit or delete it.

Two properties are load-bearing and pinned below:

* PF-1 (comment-existence oracle): a FOREIGN-authored comment and a
  NONEXISTENT comment must yield the SAME message. The wording must not
  confirm the comment exists, and it must never interpolate the
  authoring agent's id (the DB layer's ``owned by {author!r}`` string).
  Only the static author-only policy hint is added.
* The rejection stays a typed :class:`NotFound` (REST → 404,
  ``resource="task comment"``) so the existing typed-contract guards in
  ``test_wave6_pr1_small_tools.py`` still hold.

These tests drive the impls through ``dispatch_tool_call`` (the same
path a worker's MCP call takes) with hand-built principals, mirroring
``test_wave6_pr1_small_tools.py``.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import (
    NotFound,
    render_as_text_content,
    tool_result_error_message,
)
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio

# The FUSED policy hint the worker must see appended after "not found".
# Kept in lock-step with ``task_comments_tools._AUTHOR_ONLY_HINT``.
_EXPECTED_HINT = (
    ", or you are not its author. Only a comment's original author "
    "(or an admin) can edit or delete it."
)


def _agent_principal(agent_id: str, *, bearer: str, role: str | None = None) -> Principal:
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=role,  # type: ignore[arg-type]
        can_wake_loop=False,
        source_token=bearer,
    )


def _insert_task(task_id: str) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO tasks "
            "(task_id, title, description, status, created_at, "
            "updated_at, priority, parent_task, child_tasks, "
            "depends_on_tasks, notes, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, "demo", "", "pending", now, now, "medium",
                None, "[]", "[]", "[]", "admin",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _rendered_text(result) -> str:
    """The single MCP text block a worker's client receives."""
    blocks = render_as_text_content(result)
    return "".join(b.text for b in blocks)


# ── edit_task_comment ────────────────────────────────────────────


async def test_edit_foreign_note_message_adds_author_only_hint(tmp_path) -> None:
    """A worker editing a comment authored by someone else sees the
    fused author-only hint — not a bare "not found" that reads as a
    bug."""
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        _insert_task("wm-edit-foreign")
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        note_id = task_comments_db.add_comment("wm-edit-foreign", "alice", "v1")

        p = _agent_principal("bob", bearer=bob.token)
        result = await dispatch_tool_call(
            "edit_task_comment",
            {"note_id": note_id, "text": "hijack"},
            principal=p,
        )

        assert isinstance(result, NotFound), f"expected NotFound, got {result!r}"
        assert result.resource == "task comment"
        assert result.identifier == str(note_id)

        msg = tool_result_error_message(result)
        assert msg == f"task comment '{note_id}' not found{_EXPECTED_HINT}", msg
        assert "or you are not its author" in msg
        assert "Only a comment's original author (or an admin) can edit or delete it." in msg

        # PF-1: the authoring agent id must never leak, in the typed
        # object or in any rendered surface.
        assert "alice" not in repr(result)
        assert "alice" not in msg
        assert "alice" not in _rendered_text(result)
        assert "owned by" not in msg
        # Comment left untouched.
        assert task_comments_db.get_comment(note_id)["text"] == "v1"


async def test_edit_foreign_and_missing_yield_same_message(tmp_path) -> None:
    """PF-1 comment-existence oracle: a FOREIGN-authored comment and a
    NONEXISTENT comment produce the SAME opaque message (modulo the id
    the caller already supplied). A worker can't use the wording to
    confirm a foreign comment exists, and no author id leaks."""
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        _insert_task("wm-edit-oracle")
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        foreign_id = task_comments_db.add_comment("wm-edit-oracle", "alice", "v1")
        missing_id = 999999

        p = _agent_principal("bob", bearer=bob.token)
        foreign = await dispatch_tool_call(
            "edit_task_comment", {"note_id": foreign_id, "text": "x"}, principal=p,
        )
        missing = await dispatch_tool_call(
            "edit_task_comment", {"note_id": missing_id, "text": "x"}, principal=p,
        )

        assert isinstance(foreign, NotFound) and isinstance(missing, NotFound)
        # Same policy hint verbatim on both branches.
        assert foreign.hint == missing.hint == _EXPECTED_HINT

        msg_foreign = tool_result_error_message(foreign)
        msg_missing = tool_result_error_message(missing)
        # Identical message once the caller-supplied id is normalised —
        # the ONLY difference is the id the caller already knows.
        assert (
            msg_foreign.replace(str(foreign_id), "<id>")
            == msg_missing.replace(str(missing_id), "<id>")
        )
        assert "alice" not in msg_foreign and "alice" not in msg_missing


# ── delete_task_comment ───────────────────────────────────────────


async def test_delete_foreign_note_message_adds_author_only_hint(tmp_path) -> None:
    """delete_task_comment carries the same fused author-only wording."""
    from agent_mcp.db.actions import task_comments_db
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        _insert_task("wm-del-foreign")
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        note_id = task_comments_db.add_comment("wm-del-foreign", "alice", "keep")

        p = _agent_principal("bob", bearer=bob.token)
        result = await dispatch_tool_call(
            "delete_task_comment", {"note_id": note_id}, principal=p,
        )

        assert isinstance(result, NotFound), f"expected NotFound, got {result!r}"
        assert result.resource == "task comment"
        msg = tool_result_error_message(result)
        assert msg == f"task comment '{note_id}' not found{_EXPECTED_HINT}", msg
        assert "alice" not in msg and "owned by" not in msg
        # Comment not deleted.
        assert task_comments_db.get_comment(note_id) is not None
