"""``rotate_agent_token`` — credential rotation that keeps the identity.

Fix 2 of the three-flagged-decisions plan.
``AgentRepository.rotate_token`` implemented rotate-with-cache-rekey
(mint a new bearer, re-key ``state.active_agents``, write the new row)
but had ZERO callers: the only way to respond to a suspected leaked
agent token was ``terminate_agent``, which destroys the identity (task
assignments, message attribution, audit trail) rather than replacing
its credential.

This file pins the new caller — the ``rotate_agent_token`` MCP tool and
its ``POST /api/agents/<id>/rotate-token`` REST adapter:

1. the impl exists and is importable;
2. a principal WITHOUT ``agents.rotate_token`` is denied
   (``AuthRejected``) BEFORE the DB is touched — the new capability is
   distinct from ``agents.terminate``, so terminate-authority does not
   silently imply rotate-authority;
3. after rotation the OLD bearer no longer authenticates and the NEW
   one does, while ``agent_id`` / ``agent_role`` / task assignments /
   message attribution survive untouched (the whole point — contrast
   ``terminate_agent``);
4. the ``rotated_agent_token`` audit row carries only token SUFFIXES,
   never a full plaintext bearer (same discipline
   ``agent_actions_db``'s ``source_token_suffix`` already applies).

There is no overlap / grace-period window by design: an agent bearer
authenticates one long-lived stateful connection, so the swap is
atomic — the old token stops working the instant the UPDATE commits.

Note on "group memberships": the per-project schema has no
agent↔group table (groups are a router-side *user* concept). The
identity-survival assertions therefore cover the per-project analogues
an agent actually owns — its row fields, its assigned tasks, and its
message attribution.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import make_principal, mcp_session, with_capabilities

pytestmark = pytest.mark.asyncio


ROTATE_CAP = "agents.rotate_token"


# ── raw-DB helpers (bypass the tool surface) ────────────────────────


def _agent_row(agent_id: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _rotation_audit_rows(agent_id: str) -> list[dict]:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM agent_actions WHERE action_type = ? "
            "ORDER BY rowid",
            ("rotated_agent_token",),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return [
        r for r in rows
        if agent_id in (r.get("details") or "")
    ]


def _insert_task(task_id: str, created_by: str, assigned_to: str) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (task_id, title, description, status, "
            "priority, created_at, updated_at, created_by, assigned_to, "
            "notes) VALUES (?, ?, '', 'in_progress', 'medium', ?, ?, ?, ?, "
            "'[]')",
            (task_id, f"task {task_id}", now, now, created_by, assigned_to),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_message(sender: str, recipient: str) -> str:
    from agent_mcp.db.connection import get_db_connection

    msg_id = f"msg_{secrets.token_hex(6)}"
    ts = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agent_messages (message_id, sender_id, "
            "recipient_id, message_content, message_type, priority, "
            "timestamp, delivered, read) "
            "VALUES (?, ?, ?, 'hello', 'text', 'normal', ?, 0, 0)",
            (msg_id, sender, recipient, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return msg_id


# ── 1. the impl exists ──────────────────────────────────────────────


async def test_rotate_agent_token_tool_impl_is_importable() -> None:
    """RED-1: before Fix 2 there was no caller for
    ``AgentRepository.rotate_token`` at all — this import failed."""
    from agent_mcp.tools.admin_tools import rotate_agent_token_tool_impl

    assert callable(rotate_agent_token_tool_impl)
    assert (
        getattr(rotate_agent_token_tool_impl, "_required_capability", None)
        == ROTATE_CAP
    )


async def test_rotate_agent_token_is_a_registered_tool() -> None:
    """The tool is in the catalogue with ``requires=Cap(...)`` matching
    the impl's decorator (``register_tool`` verifies the two agree at
    import time and raises otherwise, so a successful registration IS
    the proof that catalogue and gate cannot disagree)."""
    import agent_mcp.tools  # noqa: F401 - import registers every tool
    from agent_mcp.tools import registry
    from agent_mcp.tools.admin_tools import rotate_agent_token_tool_impl

    assert registry.tool_implementations["rotate_agent_token"] is (
        rotate_agent_token_tool_impl
    )
    # Operator tier is DERIVED from the cap (it is in neither agent
    # bundle), so the tool never advertises itself in a worker's,
    # manager's or anonymous caller's tools/list.
    from agent_mcp.tools.access import is_visible_to_role

    assert is_visible_to_role("rotate_agent_token", "admin")
    for role in ("manager", "worker", "anonymous"):
        assert not is_visible_to_role("rotate_agent_token", role), role


# ── 2. the capability is distinct, and gates before any DB write ────


async def test_rotate_token_capability_is_in_the_locked_vocabulary() -> None:
    from agent_mcp.core.capabilities import (
        AGENT_ROLE_BUNDLES,
        KNOWN_CAPABILITIES,
        PROJECT_ROLE_BUNDLES,
    )

    assert ROTATE_CAP in KNOWN_CAPABILITIES
    # Operator tier only: minting a fresh credential for an identity
    # that KEEPS all its grants is at least as sensitive as destroying
    # it, so it lands in the same bundle as ``agents.terminate`` and
    # nowhere weaker.
    assert ROTATE_CAP in PROJECT_ROLE_BUNDLES["operator"]
    assert ROTATE_CAP not in PROJECT_ROLE_BUNDLES["viewer"]
    for bundle in AGENT_ROLE_BUNDLES.values():
        assert ROTATE_CAP not in bundle


async def test_terminate_authority_does_not_imply_rotate_authority(
    tmp_path,
) -> None:
    """RED-2 (the policy half): a principal holding ONLY
    ``agents.terminate`` is denied. The two caps are deliberately
    separate — see the plan's Fix 2 rationale."""
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.admin_tools import rotate_agent_token_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("rot-terminate-only")
        before = _agent_row("rot-terminate-only")

        with pytest.raises(AuthRejected) as excinfo:
            await rotate_agent_token_tool_impl(
                {"agent_id": "rot-terminate-only"},
                principal=with_capabilities("agents.terminate"),
            )
        assert ROTATE_CAP in str(excinfo.value)

        after = _agent_row("rot-terminate-only")
        assert after["token"] == before["token"] == worker.token, (
            "denial must happen BEFORE any DB write"
        )


async def test_worker_bearer_is_denied_before_any_db_write(
    tmp_path,
) -> None:
    """RED-2: an unprivileged agent bearer cannot rotate anyone's
    credential (least of all its own)."""
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.admin_tools import rotate_agent_token_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("rot-worker-denied")
        before = _agent_row("rot-worker-denied")

        with pytest.raises(AuthRejected):
            await rotate_agent_token_tool_impl(
                {"agent_id": "rot-worker-denied"},
                principal=make_principal(
                    kind="agent_bearer",
                    agent_id="rot-worker-denied",
                    agent_role="worker",
                ),
            )

        assert _agent_row("rot-worker-denied")["token"] == before["token"]
        assert worker.token == before["token"]


# ── 3. atomic swap; identity survives ───────────────────────────────


async def test_rotation_swaps_the_bearer_and_preserves_identity(
    tmp_path,
) -> None:
    """RED-3: after rotation the OLD bearer misses and the NEW one
    resolves, while every non-credential attribute of the identity is
    byte-for-byte unchanged."""
    from agent_mcp.core import globals as g
    from agent_mcp.core.auth import get_agent_id
    from agent_mcp.core.tool_result import Ok
    from agent_mcp.tools.admin_tools import rotate_agent_token_tool_impl

    async with mcp_session(tmp_path) as admin:
        agent_id = "rot-identity"
        worker = await admin.create_worker(agent_id)
        old_token = worker.token

        _insert_task("task_rot_1", created_by=agent_id, assigned_to=agent_id)
        msg_id = _insert_message(agent_id, "admin")

        before = _agent_row(agent_id)
        assert get_agent_id(old_token) == agent_id

        result = await rotate_agent_token_tool_impl(
            {"agent_id": agent_id},
            principal=with_capabilities(ROTATE_CAP),
        )
        assert isinstance(result, Ok), result
        new_token = result.data["token"]
        assert result.data["agent_id"] == agent_id
        assert new_token != old_token
        assert new_token

        # --- credential: atomic swap, no grace window --------------
        after = _agent_row(agent_id)
        assert after["token"] == new_token
        assert get_agent_id(new_token) == agent_id
        assert get_agent_id(old_token) is None, (
            "the OLD bearer must stop authenticating the instant the "
            "rotation commits — there is no grace period by design"
        )
        assert old_token not in g.active_agents
        assert g.active_agents[new_token]["agent_id"] == agent_id

        # --- identity: everything else survives --------------------
        for field in (
            "agent_id", "agent_role", "created_at", "color",
            "working_directory", "status", "current_task",
        ):
            assert after[field] == before[field], field

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT assigned_to, created_by FROM tasks WHERE task_id = ?",
                ("task_rot_1",),
            )
            task = cur.fetchone()
            assert task["assigned_to"] == agent_id
            assert task["created_by"] == agent_id
            cur.execute(
                "SELECT sender_id FROM agent_messages WHERE message_id = ?",
                (msg_id,),
            )
            assert cur.fetchone()["sender_id"] == agent_id
        finally:
            conn.close()


async def test_rotating_an_unknown_agent_is_not_found(tmp_path) -> None:
    from agent_mcp.core.tool_result import NotFound
    from agent_mcp.tools.admin_tools import rotate_agent_token_tool_impl

    async with mcp_session(tmp_path):
        result = await rotate_agent_token_tool_impl(
            {"agent_id": "no-such-agent-rot"},
            principal=with_capabilities(ROTATE_CAP),
        )
        assert isinstance(result, NotFound), result


async def test_rotating_a_terminated_agent_conflicts(tmp_path) -> None:
    """A terminated agent's bearer is already revoked; re-minting one
    would silently resurrect a credential for a dead identity. Restore
    first."""
    from agent_mcp.core.tool_result import Conflict
    from agent_mcp.tools.admin_tools import (
        rotate_agent_token_tool_impl,
        terminate_agent_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("rot-terminated")
        term = await terminate_agent_tool_impl(
            {"agent_id": "rot-terminated"},
            principal=with_capabilities("agents.terminate"),
        )
        assert term.__class__.__name__ == "Ok", term

        result = await rotate_agent_token_tool_impl(
            {"agent_id": "rot-terminated"},
            principal=with_capabilities(ROTATE_CAP),
        )
        assert isinstance(result, Conflict), result


# ── 4. the audit row never carries a plaintext bearer ───────────────


async def test_audit_row_records_suffixes_never_plaintext_tokens(
    tmp_path,
) -> None:
    """RED-4: the ``rotated_agent_token`` audit row must be greppable
    by an operator (which credential was replaced?) WITHOUT re-leaking
    either bearer into a table the audit-log UI renders."""
    from agent_mcp.core.tool_result import Ok
    from agent_mcp.tools.admin_tools import rotate_agent_token_tool_impl

    async with mcp_session(tmp_path) as admin:
        agent_id = "rot-audit"
        worker = await admin.create_worker(agent_id)
        old_token = worker.token

        result = await rotate_agent_token_tool_impl(
            {"agent_id": agent_id},
            principal=with_capabilities(ROTATE_CAP),
        )
        assert isinstance(result, Ok), result
        new_token = result.data["token"]

        rows = _rotation_audit_rows(agent_id)
        assert len(rows) == 1, rows
        details_raw = rows[0]["details"] or ""
        assert old_token not in details_raw
        assert new_token not in details_raw

        details = json.loads(details_raw)
        assert details["agent_id"] == agent_id
        assert details["old_token_suffix"] == old_token[-4:]
        assert details["new_token_suffix"] == new_token[-4:]

        # Nothing anywhere in the agent_actions table holds a full
        # bearer — sweep every column of every row, not just ours.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM agent_actions")
            blob = json.dumps([dict(r) for r in cur.fetchall()])
        finally:
            conn.close()
        assert old_token not in blob
        assert new_token not in blob


async def test_rotation_event_fanout_never_carries_the_plaintext_token(
    tmp_path,
) -> None:
    """The ``agent.updated`` fan-out must not ship the new bearer.

    Found while giving ``rotate_token`` its first caller. The repo's
    post-commit publish carried ``{"field": "token", "value":
    <new_token>}``; ``event_bus.StreamingQueueAdapter`` fans that
    straight out to the agent's open ``GET /mcp`` streams — and those
    streams were authenticated with the OLD bearer, the one being
    revoked. So an attacker holding the leaked token, parked on a
    stream, would be HANDED its replacement by the very operation
    meant to lock them out (``close_streams_for_agent`` only signals a
    re-validate; the teardown is not synchronous with the fan-out).
    Dead code until Fix 2, live the moment it got a caller — so the
    payload now carries a suffix, never the secret.
    """
    from agent_mcp.core import event_bus
    from agent_mcp.core.tool_result import Ok
    from agent_mcp.tools.admin_tools import rotate_agent_token_tool_impl

    seen: list[tuple[str, str, dict]] = []

    class _Capture:
        def deliver(self, agent_id, event_type, payload):
            seen.append((agent_id, event_type, dict(payload or {})))

    event_bus.register("test-rotate-capture", _Capture())
    try:
        async with mcp_session(tmp_path) as admin:
            agent_id = "rot-fanout"
            worker = await admin.create_worker(agent_id)
            old_token = worker.token

            result = await rotate_agent_token_tool_impl(
                {"agent_id": agent_id},
                principal=with_capabilities(ROTATE_CAP),
            )
            assert isinstance(result, Ok), result
            new_token = result.data["token"]
    finally:
        event_bus.unregister("test-rotate-capture")

    updates = [
        p for (aid, evt, p) in seen
        if aid == agent_id and evt == "agent.updated"
        and p.get("field") == "token"
    ]
    assert updates, f"expected an agent.updated/token event; saw {seen!r}"
    blob = json.dumps(seen)
    assert new_token not in blob, "new bearer leaked onto the event bus"
    assert old_token not in blob, "old bearer leaked onto the event bus"
    assert updates[0]["token_suffix"] == new_token[-4:]


# ── the real MCP wire ───────────────────────────────────────────────


async def test_mcp_wire_rotation_end_to_end(tmp_path) -> None:
    """Drive the tool through the registered ``tools/call`` handler —
    the same path a real JSON-RPC client takes — and prove the rotated
    agent can still work afterwards under its NEW bearer while its OLD
    one is dead."""
    from agent_mcp.core.auth import get_agent_id
    from tests.harness import WorkerSession

    async with mcp_session(tmp_path) as admin:
        agent_id = "rot-wire"
        worker = await admin.create_worker(agent_id)
        old_token = worker.token

        blocks = await admin.assert_tool_succeeds(
            "rotate_agent_token", {"agent_id": agent_id},
        )
        # Ok(message=..., data=...) renders as two blocks: prose, then
        # the JSON payload. The token rides the DATA block (the exact
        # bug ``register_agent`` hit when data was dropped) — and the
        # prose must never carry it.
        assert len(blocks) == 2, blocks
        payload = json.loads(blocks[1].text)
        new_token = payload["token"]
        assert payload["agent_id"] == agent_id
        assert new_token != old_token
        assert new_token not in blocks[0].text
        assert old_token not in blocks[0].text

        assert get_agent_id(old_token) is None
        assert get_agent_id(new_token) == agent_id

        # The identity keeps working — a session bound to the NEW bearer
        # is the SAME agent and can still use the wire.
        rotated = WorkerSession(
            token=new_token, agent_id=agent_id, _admin=admin,
        )
        await rotated.assert_tool_succeeds("view_tasks", {})


async def test_mcp_wire_worker_cannot_rotate(tmp_path) -> None:
    """A worker bearer calling the tool over the wire gets the
    Unauthorized shape — and the tool is not even in its tools/list."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("rot-wire-worker")
        before = worker.token

        await worker.assert_unauthorized(
            "rotate_agent_token", {"agent_id": "rot-wire-worker"},
        )
        assert _agent_row("rot-wire-worker")["token"] == before

        listed = {t.name for t in await worker.list_tools()}
        assert "rotate_agent_token" not in listed


# ── REST adapter ────────────────────────────────────────────────────


async def test_rest_route_rotates_and_returns_the_new_token_once(
    tmp_path,
) -> None:
    """``POST /api/agents/<id>/rotate-token`` is the same one
    implementation behind a thin adapter — shown-once contract, same
    as ``POST /api/agents/register``."""
    from agent_mcp.core.auth import get_agent_id

    async with mcp_session(tmp_path) as admin:
        agent_id = "rot-rest"
        worker = await admin.create_worker(agent_id)
        old_token = worker.token

        resp = admin.post(f"/api/agents/{agent_id}/rotate-token", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        new_token = body["agent_token"]
        assert body["agent_id"] == agent_id
        assert new_token and new_token != old_token

        assert get_agent_id(new_token) == agent_id
        assert get_agent_id(old_token) is None

        # The token is shown ONCE: no later read-back surface returns it.
        again = admin.post(f"/api/agents/{agent_id}/rotate-token", json={})
        assert again.status_code == 200, again.text
        assert again.json()["agent_token"] != new_token


async def test_rest_route_404s_for_an_unknown_agent(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        resp = admin.post("/api/agents/nope-rot/rotate-token", json={})
        assert resp.status_code == 404, resp.text


async def test_rest_route_denies_a_forwarding_viewer(tmp_path) -> None:
    """A viewer-role forwarding caller passes
    ``require_operator_session`` but lacks ``agents.rotate_token`` —
    403, and the credential is untouched."""
    async with mcp_session(tmp_path) as admin:
        agent_id = "rot-viewer-denied"
        worker = await admin.create_worker(agent_id)

        resp = admin.client.post(
            f"/api/agents/{agent_id}/rotate-token",
            json={},
            headers=admin.forwarding_header(role="viewer"),
        )
        assert resp.status_code == 403, resp.text
        assert _agent_row(agent_id)["token"] == worker.token
