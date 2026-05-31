"""Worker→worker messaging — gated by config_allow_worker_to_worker.

UPSTREAM_ISSUES.md issue K. Today `send_agent_message` always denies
worker→worker with "Communication not permitted between these agents"
because the policy check in `_can_agents_communicate` (line 54)
inspects `g.active_agents` by agent_id while the dict is actually
keyed by token — so the check never succeeds for non-admin senders.

Fix:
1. Fix the policy lookup to iterate active_agents by agent_id.
2. Gate worker→worker on a per-project `config_allow_worker_to_worker`
   key in project_context (default: deny, preserving upstream behavior
   per Q6b.1).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import secrets


def _seed_worker(name: str = "alice"):
    """Register a worker. Returns (token, agent_id)."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    worker_token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (worker_token, name, "[]", now, "active", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()

    g.active_agents[worker_token] = {
        "agent_id": name,
        "status": "active",
        "created_at": now,
        "capabilities": [],
    }
    return worker_token, name


def _set_toggle(client, key: str, value: str, admin_token: str) -> None:
    # Use the REST memory endpoint to seed/update.
    r = client.post(
        "/api/memories",
        json={"token": admin_token, "context_key": key, "context_value": value},
    )
    if r.status_code == 409:
        r = client.request(
            "PUT",
            f"/api/memories/{key}",
            json={"token": admin_token, "context_value": value},
        )
    assert r.status_code == 200, r.text


def _send(sender_token: str, recipient_id: str, body: str = "hi"):
    from agent_mcp.tools.agent_communication_tools import send_agent_message_tool_impl

    return asyncio.run(
        send_agent_message_tool_impl({
            "token": sender_token,
            "recipient_id": recipient_id,
            "message": body,
            "deliver_method": "store",  # no tmux
        })
    )


def test_admin_to_worker_still_works(client) -> None:
    """Baseline — admin to worker is unaffected by the toggle."""
    admin = client.get("/api/tokens").json()["admin_token"]
    _seed_worker("alice")
    res = _send(admin, "alice", "hello from admin")
    text = res[0].text
    assert "denied" not in text.lower(), text


def test_worker_to_worker_default_denied(client) -> None:
    """With no toggle set, worker→worker denied (preserves upstream)."""
    _seed_worker("alice")
    bob_token, _ = _seed_worker("bob")

    res = _send(bob_token, "alice", "hi alice")
    text = res[0].text
    assert "denied" in text.lower() or "not permitted" in text.lower(), (
        f"expected denial; got: {text}"
    )


def test_worker_to_worker_allowed_when_toggle_on(client) -> None:
    """With config_allow_worker_to_worker=true, worker→worker allowed."""
    admin = client.get("/api/tokens").json()["admin_token"]
    _set_toggle(client, "config_allow_worker_to_worker", "true", admin)

    _seed_worker("alice")
    bob_token, _ = _seed_worker("bob")

    res = _send(bob_token, "alice", "hi alice")
    text = res[0].text
    assert "denied" not in text.lower() and "not permitted" not in text.lower(), (
        f"expected allow with toggle on; got: {text}"
    )
