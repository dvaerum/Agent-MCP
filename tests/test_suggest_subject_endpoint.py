"""REST block: subject + parent_message_id on /api/messages*; new
/api/messages/suggest-subject endpoint.

RED test for behavior block 3 (v5.0.22). Covers four routes:

* POST /api/messages — accepts new `subject` and `parent_message_id`
  fields in the request body and persists them.
* POST /api/messages/query — returns the new columns on every row.
* POST /api/messages/suggest-subject — new endpoint:
    - With AGENT_MCP_SUBJECT_MODEL set + mocked helper returns
      `{"subject": "<value>"}`.
    - With AGENT_MCP_SUBJECT_MODEL unset returns `{"subject": null}`
      (graceful degrade, NOT 503). The dashboard renders the input
      empty and the user types one in manually.
    - Without a valid token returns 401.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ---------- POST /api/messages: new fields land in DB ------------------------


async def test_create_message_persists_subject_and_parent(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        r = admin.client.post(
            "/api/messages",
            json={
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message_content": "root body",
                "subject": "Initial Topic",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True, body
        root_id = body["message_id"]

        # Reply — supply parent_message_id; subject should be ignored
        # (replies always end up with subject NULL).
        r2 = admin.client.post(
            "/api/messages",
            json={
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message_content": "reply body",
                "parent_message_id": root_id,
                # Sender attempts to set a subject on a reply — the
                # contract is that the server force-NULLs it.
                "subject": "ignored",
            },
        )
        assert r2.status_code == 200, r2.text
        reply_id = r2.json()["message_id"]

        # Query: returned rows include the new columns.
        q = admin.client.post(
            "/api/messages/query",
            json={"token": admin.admin_token},
        )
        assert q.status_code == 200, q.text
        rows = {row["message_id"]: row for row in q.json()["messages"]}
        assert root_id in rows, rows
        assert reply_id in rows, rows

        root_row = rows[root_id]
        reply_row = rows[reply_id]
        assert "subject" in root_row, root_row
        assert "parent_message_id" in root_row, root_row
        assert root_row["subject"] == "Initial Topic"
        assert root_row["parent_message_id"] is None
        assert reply_row["parent_message_id"] == root_id
        assert reply_row["subject"] is None, (
            f"reply subject should be NULL; got {reply_row['subject']!r}"
        )


# ---------- POST /api/messages/suggest-subject -------------------------------


async def test_suggest_subject_returns_helper_value(tmp_path, monkeypatch) -> None:
    """With AGENT_MCP_SUBJECT_MODEL set + helper returning a string,
    the endpoint returns {"subject": "<value>"}."""
    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")

    async def _mock(content: str) -> str | None:
        assert content == "please help me debug this build issue", content
        return "Build debug help"

    from agent_mcp.features import message_suggestions
    monkeypatch.setattr(message_suggestions, "suggest_subject", _mock)

    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/messages/suggest-subject",
            json={
                "token": admin.admin_token,
                "content": "please help me debug this build issue",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"subject": "Build debug help"}, body


async def test_suggest_subject_unconfigured_returns_null(
    tmp_path, monkeypatch
) -> None:
    """Without AGENT_MCP_SUBJECT_MODEL set the endpoint returns
    {"subject": null} (200, not 503) so the dashboard degrades
    gracefully — the user types a subject manually."""
    monkeypatch.delenv("AGENT_MCP_SUBJECT_MODEL", raising=False)

    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/messages/suggest-subject",
            json={
                "token": admin.admin_token,
                "content": "anything here",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"subject": None}, body


async def test_suggest_subject_requires_token(tmp_path) -> None:
    """No token = 401. Per the routes convention, missing/invalid
    auth returns 401 before any helper call."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/messages/suggest-subject",
            json={"content": "anything"},
        )
        assert r.status_code == 401, (r.status_code, r.text)
