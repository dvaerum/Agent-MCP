"""Round-10 finding F1 — dashboard message-send silently drops the
message and reports a false success.

``create_message_api_route`` (``POST /api/messages``) type-guards
``recipient_id`` and ``message_content`` (round-8 PF-R8-1 fix) but NOT
``subject`` / ``parent_message_id``. A structured JSON value (dict/list)
for either field passes through unchanged, reaches
``message_repository.send(connection=cursor)`` and the SQLite bind
raises ``sqlite3.ProgrammingError`` — which ``send`` CATCHES internally
and turns into a ``None`` return. The route then IGNORED that return,
committed the ``sent_message_via_dashboard`` audit-log INSERT, and
returned ``200 {"success": true, "message_id": ...}`` — but the message
row was NEVER stored. A false "sent" audit entry lands and the operator
is told the message went through when it did not.

Two-part fix pinned here:
  1. Type-guard ``subject`` / ``parent_message_id`` (allow ``None``) →
     reject a non-string value with a 400 before ``send()``.
  2. Belt-and-suspenders: the route checks ``send()``'s return value —
     ``None`` means the store failed, so it must NOT commit a success
     audit entry and must return 500 (rollback), never a false 200.

The load-bearing invariant: after ANY 2xx from this endpoint the
message MUST be retrievable. A "success" the query can't find is the
bug.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _post(admin, **fields):
    body = {}
    body.update(fields)
    return admin.post("/api/messages", json=body)


def _query_ids(admin) -> list[str]:
    r = admin.post(
        "/api/messages/query", json={}
    )
    assert r.status_code == 200, r.text
    return [m["message_id"] for m in r.json()["messages"]]


# ---------- F1: non-string subject / parent → 400, never false 200 ----


@pytest.mark.parametrize("bad", [{"a": 1}, ["a"]])
async def test_send_non_string_subject_is_400(tmp_path, bad) -> None:
    """A dict/list ``subject`` reaches a SQLite bind that ``send``
    swallows into a ``None`` → false 200. Must be a 400 up front."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post(
            admin, recipient_id="alice", message_content="hi", subject=bad
        )
        assert r.status_code == 400, r.text
        assert "error" in r.json(), r.text


@pytest.mark.parametrize("bad", [{"a": 1}, ["a"]])
async def test_send_non_string_parent_message_id_is_400(
    tmp_path, bad
) -> None:
    """Same silent-drop path via ``parent_message_id``."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post(
            admin,
            recipient_id="alice",
            message_content="hi",
            parent_message_id=bad,
        )
        assert r.status_code == 400, r.text
        assert "error" in r.json(), r.text


@pytest.mark.parametrize(
    "fields",
    [
        {"subject": {"a": 1}},
        {"subject": ["a"]},
        {"parent_message_id": {"a": 1}},
        {"parent_message_id": ["a"]},
    ],
)
async def test_any_2xx_message_is_retrievable(tmp_path, fields) -> None:
    """The core invariant: if this endpoint answers 2xx it MUST have
    stored a retrievable message. On origin/main a dict subject returns
    200 with a ``message_id`` the query can't find — the false success
    this test forbids."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post(
            admin, recipient_id="alice", message_content="hi", **fields
        )
        if 200 <= r.status_code < 300:
            mid = r.json().get("message_id")
            assert mid is not None, r.text
            assert mid in _query_ids(admin), (
                "endpoint reported 2xx success but the message is not "
                f"retrievable (silent drop): {r.text}"
            )


async def test_failed_store_yields_500_no_false_audit(
    tmp_path, monkeypatch
) -> None:
    """Belt-and-suspenders: if ``send()`` returns ``None`` (store
    failed) the route must NOT commit a success audit entry and must
    return 500 — never a false 200."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        from agent_mcp.repositories import get_message_repo

        repo = get_message_repo()
        monkeypatch.setattr(repo, "send", lambda **kw: None)

        r = _post(admin, recipient_id="alice", message_content="hi")
        assert r.status_code == 500, r.text
        assert r.json().get("success") is not True, r.text

        # No false "sent" audit entry may have been committed.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM agent_actions "
                "WHERE action_type = 'sent_message_via_dashboard'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 0, (
            f"failed store still committed {n} false 'sent' audit "
            f"entr(y/ies)"
        )


# ---------- regression: valid subject + parent still stores ----------


async def test_valid_subject_and_parent_still_stores(tmp_path) -> None:
    """Happy path intact: a root message with a valid string subject
    stores + returns a message_id, and a reply carrying a valid string
    ``parent_message_id`` stores too — both retrievable."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        root = _post(
            admin,
            recipient_id="alice",
            message_content="root body",
            subject="a real subject",
        )
        assert root.status_code == 200, root.text
        root_id = root.json()["message_id"]
        assert root_id in _query_ids(admin)

        reply = _post(
            admin,
            recipient_id="alice",
            message_content="reply body",
            parent_message_id=root_id,
        )
        assert reply.status_code == 200, reply.text
        reply_id = reply.json()["message_id"]
        assert reply_id in _query_ids(admin)
