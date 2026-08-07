"""Phase 2: deferred batched backfill of null message subjects.

Phase 1 (PR #554) made the send path store ``subject = NULL`` for a root
message sent without an explicit subject — the marker for "no real subject
set" — and moved subject generation off the synchronous send path. Phase 2
is the asynchronous half: a background sweep that titles the NULL-subject
backlog in BATCHES, decoupled from message sends so the (RAM-hungry,
socket-activated) llama-cpp model is loaded once per sweep and amortised
across many messages.

Locked behaviour exercised here:

* ``backfill_null_subjects`` finds ROOT messages needing a subject
  (``parent_message_id IS NULL AND subject IS NULL``), and for each up to a
  batch limit calls ``await suggest_subject(content)``:
    - non-empty subject  -> UPDATE the row (NULL -> real).
    - ``None``           -> leave NULL (retried next sweep).
* Replies (``parent_message_id`` set) are NEVER titled, even with a NULL
  subject — they are subjectless by design.
* A message that already has a real subject is untouched.
* The backlog drains FIFO (oldest-first).
* The sweep is a no-op when ``AGENT_MCP_SUBJECT_MODEL`` is unset (no model
  to generate with).
* After a backfill flips NULL -> real, the Phase 1 read path returns the
  row with the real subject and ``subject_is_placeholder == False`` — the
  two phases compose.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets
import sqlite3

import pytest

from tests.harness import mcp_session, seed_agent_rows

# --- direct DB helpers -------------------------------------------------------


def _seed_message(
    sender: str,
    recipient: str,
    content: str,
    *,
    subject: str | None = None,
    parent_message_id: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Insert one row directly into agent_messages, returning its id."""
    from agent_mcp.db.connection import get_db_connection

    ts = timestamp or _dt.datetime.now().isoformat()
    message_id = f"msg_{secrets.token_hex(8)}"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agent_messages (message_id, sender_id, recipient_id, "
        "message_content, message_type, priority, timestamp, delivered, read, "
        "subject, parent_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, sender, recipient, content, "text", "normal", ts, 1, 0,
         subject, parent_message_id),
    )
    conn.commit()
    conn.close()
    return message_id


def _fetch_subject(message_id: str) -> str | None:
    from agent_mcp.core.config import get_db_path

    conn = sqlite3.connect(str(get_db_path()))
    try:
        row = conn.execute(
            "SELECT subject FROM agent_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _messages_payload(content_blocks) -> list[dict]:
    """Decode the structured `messages` list from a get_agent_messages
    tool result (last JSON block that carries a `messages` key)."""
    for block in reversed(content_blocks):
        text = getattr(block, "text", "") or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "messages" in data:
            return data["messages"]
    raise AssertionError("no JSON messages payload in tool result")


# --- the sweep: backfill_null_subjects ---------------------------------------


@pytest.mark.asyncio
async def test_backfill_titles_null_subject_root(tmp_path, monkeypatch) -> None:
    """A NULL-subject ROOT gets titled: subject flips NULL -> the model's
    suggestion in the DB."""
    from agent_mcp.features import message_suggestions, subject_backfill

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "test-model")

    async def _fake(content: str) -> str | None:
        return "Fixed Subject"

    monkeypatch.setattr(message_suggestions, "suggest_subject", _fake)

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        root = _seed_message("admin", "alice", "please help with the build")

        titled = await subject_backfill.backfill_null_subjects(batch_limit=25)

        assert titled == 1, f"expected 1 titled, got {titled}"
        assert _fetch_subject(root) == "Fixed Subject"


@pytest.mark.asyncio
async def test_backfill_skips_replies(tmp_path, monkeypatch) -> None:
    """A reply (parent_message_id set, subject NULL) is NEVER titled."""
    from agent_mcp.features import message_suggestions, subject_backfill

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "test-model")

    async def _fake(content: str) -> str | None:
        return "Should Not Apply"

    monkeypatch.setattr(message_suggestions, "suggest_subject", _fake)

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        root = _seed_message("admin", "alice", "root body", subject="Root Subj")
        reply = _seed_message(
            "admin", "alice", "a reply body", parent_message_id=root,
        )

        titled = await subject_backfill.backfill_null_subjects(batch_limit=25)

        assert titled == 0, "reply must not be titled; root already had a subject"
        assert _fetch_subject(reply) is None, "reply subject must stay NULL"
        assert _fetch_subject(root) == "Root Subj", "real subject untouched"


@pytest.mark.asyncio
async def test_backfill_leaves_existing_real_subject(
    tmp_path, monkeypatch
) -> None:
    """A root that already has a real subject is untouched."""
    from agent_mcp.features import message_suggestions, subject_backfill

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "test-model")

    async def _boom(content: str) -> str | None:  # pragma: no cover
        raise AssertionError("suggest_subject must not run for a titled root")

    monkeypatch.setattr(message_suggestions, "suggest_subject", _boom)

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        root = _seed_message("admin", "alice", "body", subject="Already Titled")

        titled = await subject_backfill.backfill_null_subjects(batch_limit=25)

        assert titled == 0
        assert _fetch_subject(root) == "Already Titled"


@pytest.mark.asyncio
async def test_backfill_none_result_leaves_null(tmp_path, monkeypatch) -> None:
    """suggest_subject returning None (model unavailable) leaves the row
    NULL so it is retried next sweep."""
    from agent_mcp.features import message_suggestions, subject_backfill

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "test-model")

    async def _none(content: str) -> str | None:
        return None

    monkeypatch.setattr(message_suggestions, "suggest_subject", _none)

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        root = _seed_message("admin", "alice", "still needs a subject")

        titled = await subject_backfill.backfill_null_subjects(batch_limit=25)

        assert titled == 0
        assert _fetch_subject(root) is None, "row stays eligible for next sweep"


@pytest.mark.asyncio
async def test_backfill_respects_batch_limit_fifo(
    tmp_path, monkeypatch
) -> None:
    """With more null roots than the batch limit, only `limit` are titled per
    sweep — and the OLDEST ones first (FIFO drain)."""
    from agent_mcp.features import message_suggestions, subject_backfill

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "test-model")

    async def _fake(content: str) -> str | None:
        return f"S:{content}"

    monkeypatch.setattr(message_suggestions, "suggest_subject", _fake)

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        base = _dt.datetime(2026, 1, 1, 12, 0, 0)
        ids = []
        for i in range(5):
            ts = (base + _dt.timedelta(minutes=i)).isoformat()
            ids.append(
                _seed_message("admin", "alice", f"body{i}", timestamp=ts)
            )

        titled = await subject_backfill.backfill_null_subjects(batch_limit=2)

        assert titled == 2, f"batch limit 2 must title exactly 2, got {titled}"
        # Oldest two (i=0, i=1) titled; the rest stay NULL.
        assert _fetch_subject(ids[0]) == "S:body0"
        assert _fetch_subject(ids[1]) == "S:body1"
        assert _fetch_subject(ids[2]) is None
        assert _fetch_subject(ids[3]) is None
        assert _fetch_subject(ids[4]) is None


@pytest.mark.asyncio
async def test_backfill_noop_when_model_unset(tmp_path, monkeypatch) -> None:
    """AGENT_MCP_SUBJECT_MODEL unset -> the sweep is a no-op: nothing titled,
    the model helper is never consulted."""
    from agent_mcp.features import message_suggestions, subject_backfill

    monkeypatch.delenv("AGENT_MCP_SUBJECT_MODEL", raising=False)

    async def _boom(content: str) -> str | None:  # pragma: no cover
        raise AssertionError("suggest_subject must not run with no model set")

    monkeypatch.setattr(message_suggestions, "suggest_subject", _boom)

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        root = _seed_message("admin", "alice", "no model configured")

        titled = await subject_backfill.backfill_null_subjects(batch_limit=25)

        assert titled == 0
        assert _fetch_subject(root) is None


# --- repo methods ------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_null_subject_roots_filters_and_orders(tmp_path) -> None:
    """The repo query returns only null-subject roots, oldest-first, capped
    at the limit, with message_id + message_content."""
    from agent_mcp.repositories import message_repo

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        base = _dt.datetime(2026, 2, 1, 9, 0, 0)
        older = _seed_message(
            "admin", "alice", "older root",
            timestamp=base.isoformat(),
        )
        newer = _seed_message(
            "admin", "alice", "newer root",
            timestamp=(base + _dt.timedelta(minutes=1)).isoformat(),
        )
        # excluded: real subject
        _seed_message("admin", "alice", "titled", subject="Has Subject")
        # excluded: reply
        _seed_message(
            "admin", "alice", "reply", parent_message_id=older,
        )

        rows = message_repo.fetch_null_subject_roots(10)

        assert [r["message_id"] for r in rows] == [older, newer], rows
        assert rows[0]["message_content"] == "older root"

        limited = message_repo.fetch_null_subject_roots(1)
        assert [r["message_id"] for r in limited] == [older]


@pytest.mark.asyncio
async def test_set_message_subject_updates_row(tmp_path) -> None:
    from agent_mcp.repositories import message_repo

    async with mcp_session(tmp_path):
        seed_agent_rows("alice")
        root = _seed_message("admin", "alice", "body")

        assert message_repo.set_message_subject(root, "New Subject") is True
        assert _fetch_subject(root) == "New Subject"

        assert message_repo.set_message_subject("msg_missing", "x") is False


# --- composition with the Phase 1 read path ----------------------------------


@pytest.mark.asyncio
async def test_read_path_composes_after_backfill(tmp_path, monkeypatch) -> None:
    """End-to-end: a root sent without a subject (Phase 1 stores NULL, read
    path shows a placeholder preview) reads back with the REAL subject and
    subject_is_placeholder == False once the Phase 2 sweep titles it."""
    from agent_mcp.features import message_suggestions, subject_backfill

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "test-model")

    async def _fake(content: str) -> str | None:
        return "Backfilled Title"

    monkeypatch.setattr(message_suggestions, "suggest_subject", _fake)

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "z" * 80,
                "deliver_method": "store",
            },
        )

        # Before the sweep: placeholder preview.
        blocks = await alice.assert_tool_succeeds(
            "get_agent_messages", {"include_received": True}
        )
        row = _messages_payload(blocks)[0]
        assert row["subject_is_placeholder"] is True, row

        titled = await subject_backfill.backfill_null_subjects(batch_limit=25)
        assert titled == 1

        # After the sweep: real subject, flag off.
        blocks = await alice.assert_tool_succeeds(
            "get_agent_messages", {"include_received": True}
        )
        row = _messages_payload(blocks)[0]
        assert row["subject"] == "Backfilled Title", row
        assert row["subject_is_placeholder"] is False, row


# --- background task registration --------------------------------------------


def test_subject_backfill_task_registered_and_gated() -> None:
    """The periodic sweep must be wired into start_background_tasks, gated on
    AGENT_MCP_SUBJECT_MODEL, with a globals handle for cancellation.

    Static-source assertion (mirrors the retention pruner's registration
    test): task_group.start() reports None everywhere, so we verify the call
    site directly rather than observing a runtime cancel scope.
    """
    import inspect

    from agent_mcp.app import server_lifecycle
    from agent_mcp.core import globals as g
    from agent_mcp.features import subject_backfill

    assert hasattr(g, "subject_backfill_task_scope"), (
        "expected globals.subject_backfill_task_scope handle to exist"
    )

    assert hasattr(server_lifecycle, "run_subject_backfill_periodically"), (
        "expected server_lifecycle to import run_subject_backfill_periodically"
    )
    assert (
        server_lifecycle.run_subject_backfill_periodically
        is subject_backfill.run_subject_backfill_periodically
    ), "expected the imported symbol to be the sweep from features.subject_backfill"

    src = inspect.getsource(server_lifecycle.start_background_tasks)
    assert "run_subject_backfill_periodically" in src, (
        "expected start_background_tasks to launch the subject-backfill sweep"
    )
    assert "AGENT_MCP_SUBJECT_MODEL" in src, (
        "expected the sweep to be gated on AGENT_MCP_SUBJECT_MODEL"
    )
    assert "task_group.start" in src, (
        "expected start_background_tasks to actually start the task"
    )
