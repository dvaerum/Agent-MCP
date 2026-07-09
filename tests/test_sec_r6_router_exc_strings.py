"""SD-R6-1: generic 500 errors in sibling REST routers (completes BL-R5-2).

Round 5 (commit f314d67, "BL-R5-2") genericized ``str(e)``-on-500
exception reflection in ``app/routers/memories.py`` only. The identical
schema-disclosure pattern — reflecting a raw exception (``f"...: {str(e)}"``,
status 500) whose ``str(e)`` on a ``sqlite3.*Error`` / ``SQLAlchemyError``
embeds SQL text + bound parameters — survived in the sibling handlers:

  * tasks.py    — list (GET ""), create (POST "")
  * messages.py — list (POST /query), participants, send, patch
  * agents.py   — list (GET ""), register, restore, edit, purge, purge-preview
  * settings.py — tokens (GET /api/tokens)

This module pins one representative handler per file: force an unexpected
exception carrying an SQL-looking sentinel, then assert the 500 body is a
STATIC generic message with NO SQL text / bound params / ``str(e)`` leak.
It also asserts the deliberate ``ValueError``→400 validation branches still
return their intended user-facing messages (regression guard — those are
NOT to be genericized).

RED against origin/main (raw error leaked in the 500 body); GREEN after the
BL-R5-2 pattern is applied verbatim to the sibling handlers.
"""

from __future__ import annotations

import json as _json
import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# A sentinel that looks like leaked SQL / schema. If any of these fragments
# reach the client, the raw exception was reflected — the finding is live.
_SENTINEL_SQL = "SELECT secret_col FROM agents WHERE token = 'p@ram'"
_LEAK_FRAGMENTS = ("secret_col", "SELECT", "p@ram", "binding parameter", "sqlite3")


def _assert_no_leak(body: dict) -> None:
    """The 500 body must not carry any SQL text / bound params / str(e)."""
    blob = _json.dumps(body)
    for frag in _LEAK_FRAGMENTS:
        assert frag not in blob, (
            f"500 body leaked exception detail {frag!r}: {blob}"
        )


# ── tasks.py — create handler (live dict-as-description repro) ────────────


async def test_tasks_create_500_is_generic_on_unexpected_error(
    tmp_path, monkeypatch
) -> None:
    """When an unexpected error is raised deep in the create-task insert,
    the 500 body must be a static generic message, not the reflected
    exception detail.

    SEC round-9 note: this test formerly triggered the 500 by POSTing a
    dict ``task_description`` (which reached a SQLite bind and 500'd).
    That type-confusion path is now guarded to a clean 400 up front
    (see ``tests/test_sec_r9_rest_type_confusion.py``), so the generic-
    500 body is exercised the same way the sibling handlers below are —
    by monkeypatching the DB seam to raise an SQL-sentinel error after
    the validation guards have passed.
    """
    import agent_mcp.app.routers.tasks as tasks_router

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError(_SENTINEL_SQL)

    async with mcp_session(tmp_path) as admin:
        monkeypatch.setattr(tasks_router, "get_db_connection", _boom)
        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "leak probe",
                "task_description": "a valid string description",
            },
        )
        assert r.status_code == 500, r.text
        body = r.json()
        _assert_no_leak(body)
        assert body.get("error") == "Failed to create task", body


async def test_tasks_create_400_validation_message_preserved(tmp_path) -> None:
    """Regression: the deliberate missing-title 400 keeps its intended,
    user-facing message (must NOT be genericized)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/tasks",
            json={"token": admin.admin_token, "task_description": "no title"},
        )
        assert r.status_code == 400, r.text
        assert r.json().get("error") == "task_title is required", r.json()


# ── messages.py — list handler (POST /api/messages/query) ─────────────────


async def test_messages_list_500_is_generic_on_unexpected_error(
    tmp_path, monkeypatch
) -> None:
    """A ``sqlite3.OperationalError`` raised from the repo query must be
    caught and reported as a static generic message — the SQL sentinel it
    carries must not reach the client."""

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError(_SENTINEL_SQL)

    async with mcp_session(tmp_path) as admin:
        # ``message_repo`` is a lifespan-owned singleton resolved lazily
        # via the package ``__getattr__``; import it AFTER the lifespan has
        # installed the instance the handler will actually use.
        from agent_mcp.repositories import message_repo

        monkeypatch.setattr(message_repo, "query", _boom)
        r = admin.client.post(
            "/api/messages/query",
            json={"limit": 50, "offset": 0},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 500, r.text
        body = r.json()
        _assert_no_leak(body)
        assert body.get("error") == "Failed to list messages", body


async def test_messages_list_400_validation_message_preserved(tmp_path) -> None:
    """Regression: the deliberate limit-range 400 keeps its intended
    message."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/messages/query",
            json={"limit": 0},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 400, r.text
        assert r.json().get("error") == "limit must be 1..500", r.json()


# ── agents.py — list handler (GET /api/agents) ────────────────────────────


async def test_agents_list_500_is_generic_on_unexpected_error(
    tmp_path, monkeypatch
) -> None:
    """When the DB connection raises inside the agents-list handler, the
    500 body must be static/generic — no reflected SQL sentinel."""
    import agent_mcp.app.routers.agents as agents_router

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError(_SENTINEL_SQL)

    async with mcp_session(tmp_path) as admin:
        monkeypatch.setattr(agents_router, "get_db_connection", _boom)
        r = admin.client.get("/api/agents")
        assert r.status_code == 500, r.text
        body = r.json()
        _assert_no_leak(body)
        assert body.get("error") == "Failed to fetch agents list", body


# ── settings.py — tokens handler (GET /api/tokens) ────────────────────────


class _BoomDict(dict):
    """A real dict (so harness teardown's ``.clear()`` / ``.update()`` still
    work) whose ``.items()`` raises an SQL-looking error — models an
    unexpected failure inside the tokens handler's try block."""

    def items(self):  # type: ignore[override]
        raise sqlite3.OperationalError(_SENTINEL_SQL)


async def test_settings_tokens_500_is_generic_on_unexpected_error(
    tmp_path, monkeypatch
) -> None:
    """GET /api/tokens (confirmed operator-tier bearer) that hits an
    unexpected error must return a static generic 500 — no SQL leak."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        monkeypatch.setattr(g, "active_agents", _BoomDict())
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 500, r.text
        body = r.json()
        _assert_no_leak(body)
        assert body.get("error") == "Error retrieving tokens", body
