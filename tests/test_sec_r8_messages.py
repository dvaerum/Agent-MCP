"""Round-8 findings on the dashboard messages router.

BL-R8-1 — single-recipient dashboard message send skips the post-commit
inbox wake (lost wakeup). ``create_message_api_route`` single-recipient
branch calls ``message_repo.send(..., connection=cursor)`` which
DELIBERATELY suppresses its ``message.created`` publish while a caller
cursor is open (a subscriber must never observe an uncommitted row).
Every other send path re-fires the wake post-commit — the MCP
``send_agent_message`` tool calls ``g.notify_agent_inbox(recipient_id)``,
and the dashboard broadcast path publishes via ``bulk_send``. Only this
single-recipient dashboard path dropped it, so a recipient blocked in
``wait_for_events`` was never woken for a dashboard-composed direct
message. These tests pin the mirror: a single-recipient send wakes the
recipient's inbox, and a broadcast still wakes every recipient.

PF-R8-1 — type-confusion 500 on non-string message fields. Non-string
JSON values reached ``.strip()`` / a SQLite bind before validation:
``content`` on /suggest-subject (``(data.get('content') or "").strip()``
sits outside the route's ``try/except ValueError``), and
``recipient_id`` / ``message_content`` on the compose route (a ``dict``
passes the ``if not x`` truthiness gate, then hits a SQLite bind →
ProgrammingError → 500). These must be 400, not 500.

We spy ``notify_agent_inbox`` / ``_event_bus_shim.publish`` rather than
drive a real waiter — the contract is "wake fired for the right
recipient", decoupled from the bus/matcher internals.
"""

from __future__ import annotations

import pytest

import agent_mcp.core.globals as _g_mod
from agent_mcp.core import event_bus_shim as _shim
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _install_spies(monkeypatch):
    """Record inbox wakes + EventBus publishes. Installed AFTER worker
    setup so create_worker's own publishes don't pollute the recorders."""
    notified: list[str] = []
    published: list[tuple] = []
    monkeypatch.setattr(
        _g_mod, "notify_agent_inbox",
        lambda agent_id: notified.append(agent_id),
    )
    monkeypatch.setattr(
        _shim, "publish",
        lambda agent_id, event_type, payload: published.append(
            (agent_id, event_type, payload)
        ),
    )
    return notified, published


# ---------- BL-R8-1: post-commit inbox wake --------------------------


async def test_single_recipient_send_wakes_recipient_inbox(
    tmp_path, monkeypatch,
) -> None:
    """A dashboard-composed direct message must wake the recipient's
    inbox after commit — otherwise a wait_for_events waiter never sees
    the new row without polling."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        notified, _published = _install_spies(monkeypatch)

        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "hello alice",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True, r.text

        assert "alice" in notified, (
            f"recipient inbox not woken after single-recipient send; "
            f"notified={notified}"
        )


async def test_broadcast_still_wakes_all_recipients(
    tmp_path, monkeypatch,
) -> None:
    """Regression: a broadcast (recipient_id='*') still fans a
    message.created publish to every active worker."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        _notified, published = _install_spies(monkeypatch)

        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "*",
                "message_content": "everyone read this",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("broadcast") is True, r.text

        created = {p[0] for p in published if p[1] == "message.created"}
        assert {"alice", "bob"} <= created, (
            f"broadcast did not wake all recipients; "
            f"message.created published for={created}"
        )


# ---------- PF-R8-1: non-string fields → 400 not 500 -----------------


@pytest.mark.parametrize("bad", [{"x": 1}, [1, 2, 3]])
async def test_suggest_subject_non_string_content_is_400(
    tmp_path, bad,
) -> None:
    """A dict/list ``content`` on /suggest-subject must be a 400, not a
    500 from ``.strip()`` on a non-string."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/messages/suggest-subject",
            json={"content": bad},
        )
        assert r.status_code == 400, r.text
        assert "error" in r.json(), r.text


async def test_suggest_subject_valid_string_still_works(tmp_path) -> None:
    """Regression: a plain-string content still returns 200 with a
    ``subject`` key (null when no subject model configured)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/messages/suggest-subject",
            json={"content": "a real subject"},
        )
        assert r.status_code == 200, r.text
        assert "subject" in r.json(), r.text


@pytest.mark.parametrize("bad", [{"x": 1}, [1, 2, 3]])
async def test_send_non_string_recipient_is_400(tmp_path, bad) -> None:
    """A dict/list ``recipient_id`` passes the truthiness gate then hits
    a SQLite bind → 500. Must be 400 instead."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": bad,
                "message_content": "hi",
            },
        )
        assert r.status_code == 400, r.text
        assert "error" in r.json(), r.text


@pytest.mark.parametrize("bad", [{"x": 1}, [1, 2, 3]])
async def test_send_non_string_content_is_400(tmp_path, bad) -> None:
    """A dict/list ``message_content`` must be a 400, not a 500."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": bad,
            },
        )
        assert r.status_code == 400, r.text
        assert "error" in r.json(), r.text


async def test_send_valid_string_still_works(tmp_path) -> None:
    """Regression: valid string fields still create the message."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "hello alice",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True, r.text
