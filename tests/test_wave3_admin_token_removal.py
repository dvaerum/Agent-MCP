"""Wave 3 — drop ``admin_token`` from external surfaces.

Plan: ``/home/dennis/.claude/plans/prancy-napping-pie.md`` Wave 3.

Wave 1 (PR #203) cookie-migrated ``/api/tokens`` and ``/api/all-data``.
Wave 2 (PR #204) stripped the frontend's reads of ``admin_token`` and
finished the SSE notifications cookie migration. Wave 3 closes the
loop on the backend: the API responses no longer surface
``admin_token``, the self-RPC handlers no longer synthesise a system
bearer to satisfy a downstream ``@requires("admin")`` gate (they now
rely on the operator-session-active ContextVar that
``_dispatch_through_tool`` stamps when ``operator_session=True``), and
the ``task_notes`` privilege check moves from the raw
``token == g.admin_token`` comparison to ``verify_token(..., "manager")``.

The RED protocol per ``feedback_tdd_red_green_for_bugs``:

* Response shape: ``/api/tokens`` and ``/api/all-data`` must NOT have
  an ``admin_token`` field in the JSON body.
* Self-RPC routes: the four operator-gated dashboard adapters
  (``POST /api/agents``, ``POST /api/terminate-agent``,
  ``DELETE /api/memories/<k>``, ``DELETE /api/tasks/<id>``) must:
  - (a) succeed via the legacy admin-bearer path (backwards compat
        for admin CLI scripts)
  - (b) reject worker-bearer auth with 401
  - (c) reject anonymous auth with 401
  Cookie path is integration-covered by
  ``tests/router/test_dashboard_session_auth.py``.
* ``task_notes`` privilege: ``edit_task_note`` admits the system
  bearer (legacy admin), admits a manager-role agent token, denies a
  worker-role agent token that isn't the author, and continues to
  admit a worker-role agent token that IS the author.

The four migrated routes are the highest-risk surface in the wave —
silent privilege escalation / drop is the failure mode. The
adversarial-verification step (a second read-only review of the diff)
is captured in the PR body.
"""

from __future__ import annotations

import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ── Section 1: response payloads must not contain admin_token ─────────


async def test_tokens_endpoint_response_omits_admin_token(tmp_path) -> None:
    """``GET /api/tokens`` must not include an ``admin_token`` key.

    Pre-Wave-3 the body was ``{"admin_token": "...", "agent_tokens": [...]}``;
    Wave 2 dropped the frontend reads, Wave 3 strips the field itself.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "admin_token" not in body, (
            "Wave 3: /api/tokens must not include 'admin_token'; "
            f"body keys: {list(body.keys())}"
        )
        # Sanity: agent_tokens shape is preserved.
        assert "agent_tokens" in body, (
            f"agent_tokens must stay in the response; got keys "
            f"{list(body.keys())}"
        )


async def test_all_data_endpoint_response_omits_admin_token(tmp_path) -> None:
    """``GET /api/all-data`` must not include an ``admin_token`` key
    in the top-level response payload."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/all-data",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "admin_token" not in body, (
            "Wave 3: /api/all-data must not include 'admin_token'; "
            f"body keys: {list(body.keys())}"
        )
        # Sanity: shape otherwise unchanged.
        assert "agents" in body
        assert "tasks" in body


async def test_all_data_response_agents_section_no_admin_token(
    tmp_path,
) -> None:
    """The agents section must not carry the system bearer in any
    row's ``auth_token`` field. Pre-Wave-3 the synthesised ``Admin``
    row stored ``g.admin_token`` there; removing that synthesis
    plugs the leak.

    Caveat: the system bearer also lives in ``project_context`` under
    the ``config_system_token`` key (so it survives restarts; see
    ``application_startup``). That row is surfaced via the
    ``/api/all-data`` ``context`` section by design, and Wave 3 does
    not change that. Memories are gated behind admin auth (Wave 1 PR
    #203) so the leak is admin-to-admin only; the ``config_*`` redact
    Wave 4 may pick up is a separate decision. This test pins only
    the regression we're fixing here — no agents row's auth_token
    must equal the system bearer.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/all-data",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for entry in body.get("agents", []):
            assert entry.get("auth_token") != admin.admin_token, (
                f"agents row {entry.get('agent_id')!r} must not carry "
                f"the system bearer in auth_token"
            )


# ── Section 2: self-RPC routes use operator-session, not synthesised
#               admin bearer ─────────────────────────────────────────


async def test_create_agent_via_admin_bearer_still_works(tmp_path) -> None:
    """POST /api/agents must still work via the legacy admin-bearer
    fallback path (backwards compat for admin CLI scripts)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/agents",
            json={"agent_id": "via-admin-bearer", "capabilities": []},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text


async def test_create_agent_rejects_worker_bearer(tmp_path) -> None:
    """POST /api/agents must reject worker-bearer auth with 401.

    The route's outer dep is ``require_operator_session`` (admin-only).
    Wave 3's self-RPC rewrite must preserve this rejection — a worker
    bearer must never reach the inner ``create_agent_tool_impl``.
    """
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("worker-creator")
        r = admin.client.post(
            "/api/agents",
            json={"agent_id": "should-not-create", "capabilities": []},
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 401, r.text


async def test_create_agent_rejects_no_auth(tmp_path) -> None:
    """POST /api/agents must 401 with no auth at all."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/agents",
            json={"agent_id": "no-auth-create", "capabilities": []},
        )
        assert r.status_code == 401, r.text


async def test_terminate_agent_via_admin_bearer_still_works(tmp_path) -> None:
    """POST /api/terminate-agent — admin bearer path."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("doomed-worker")
        r = admin.client.post(
            "/api/terminate-agent",
            json={"agent_id": "doomed-worker"},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text


async def test_terminate_agent_rejects_worker_bearer(tmp_path) -> None:
    """POST /api/terminate-agent — worker bearer must 401."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("target")
        worker = await admin.create_worker("attacker")
        r = admin.client.post(
            "/api/terminate-agent",
            json={"agent_id": "target"},
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 401, r.text


async def test_terminate_agent_rejects_no_auth(tmp_path) -> None:
    """POST /api/terminate-agent — no auth must 401."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("target2")
        r = admin.client.post(
            "/api/terminate-agent",
            json={"agent_id": "target2"},
        )
        assert r.status_code == 401, r.text


async def _seed_memory(admin, key: str, value: str) -> None:
    """Seed a memory entry via the dashboard API (admin bearer path)."""
    r = admin.client.post(
        "/api/memories",
        json={"context_key": key, "context_value": value},
        headers={"Authorization": f"Bearer {admin.admin_token}"},
    )
    assert r.status_code == 200, r.text


async def test_delete_memory_via_admin_bearer_still_works(tmp_path) -> None:
    """DELETE /api/memories/<k> — admin bearer path."""
    async with mcp_session(tmp_path) as admin:
        await _seed_memory(admin, "wave3-mem-1", "hello")
        r = admin.client.request(
            "DELETE",
            "/api/memories/wave3-mem-1",
            json={},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text


async def test_delete_memory_rejects_worker_bearer(tmp_path) -> None:
    """DELETE /api/memories/<k> — worker bearer must 401."""
    async with mcp_session(tmp_path) as admin:
        await _seed_memory(admin, "wave3-mem-2", "hello")
        worker = await admin.create_worker("mem-attacker")
        r = admin.client.request(
            "DELETE",
            "/api/memories/wave3-mem-2",
            json={},
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 401, r.text


async def test_delete_memory_rejects_no_auth(tmp_path) -> None:
    """DELETE /api/memories/<k> — no auth must 401."""
    async with mcp_session(tmp_path) as admin:
        await _seed_memory(admin, "wave3-mem-3", "hello")
        r = admin.client.request(
            "DELETE", "/api/memories/wave3-mem-3", json={}
        )
        assert r.status_code == 401, r.text


async def test_delete_memory_via_operator_session_succeeds(tmp_path) -> None:
    """DELETE /api/memories/<k> — operator-session path (no body token,
    no Authorization header) must succeed.

    This is the path the dashboard takes after retire-system-token Wave 2:
    the browser holds an ``agent_mcp_session`` cookie, the router
    translates it into a signed ``X-Agent-MCP-Forwarded-Operator``
    header, and the backend's ``require_operator_session`` admits via
    the forwarding-header branch. The DELETE body is ``{}`` — no token.

    F005 (verify-all-v4) regression: the route used to dispatch
    through the ``delete_project_context`` MCP tool, which is gated
    by ``@requires("any")``. The ``"any"`` branch needs a per-agent
    token to resolve an ``agent_id`` (audit attribution), so an
    operator-session call with no bearer hit ``Unauthorized: Valid
    token required`` 403. Sibling CREATE/UPDATE routes write the
    DB directly via SQLAlchemy and didn't share the regression.

    ``admin.request(...)`` (not ``admin.client.request``) attaches the
    signed forwarding header that the router would attach in
    production — closest in-process simulation of the cookie path.
    """
    async with mcp_session(tmp_path) as admin:
        # Create the memory via the same operator-session path so we
        # also pin the sibling-route parity (CREATE works; DELETE must
        # too).
        r_create = admin.request(
            "POST",
            "/api/memories",
            json={"context_key": "f005-mem", "context_value": "hello"},
        )
        assert r_create.status_code == 200, r_create.text

        # The bug: DELETE with operator-session + no body token + no
        # Authorization header → 403 "Valid token required".
        r = admin.request(
            "DELETE", "/api/memories/f005-mem", json={}
        )
        assert r.status_code == 200, r.text

        # Confirm the row is actually gone (not just a 200 envelope).
        from agent_mcp.db.engine import SessionLocal
        from agent_mcp.db.models import ProjectContext

        sess = SessionLocal()
        try:
            row = (
                sess.query(ProjectContext)
                .filter(ProjectContext.context_key == "f005-mem")
                .one_or_none()
            )
            assert row is None, "DELETE must remove the row"
        finally:
            sess.close()


async def _seed_task(admin, task_id: str) -> None:
    """Seed a task row via direct SQL insert.

    The dashboard ``/api/tasks`` POST handler relies on the same
    auth dep, so going via the public API would be a circular test.
    Direct INSERT is the harness convention for "we need a row,
    not exercising the public-API path".
    """
    import datetime
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (task_id, title, description, status, "
            "priority, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "t", "d", "pending", "medium", now, now, "admin"),
        )
        conn.commit()
    finally:
        conn.close()


async def test_delete_task_via_admin_bearer_still_works(tmp_path) -> None:
    """DELETE /api/tasks/<id> — admin bearer path."""
    async with mcp_session(tmp_path) as admin:
        await _seed_task(admin, "wave3-task-1")
        r = admin.client.request(
            "DELETE",
            "/api/tasks/wave3-task-1",
            json={},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text


async def test_delete_task_rejects_worker_bearer(tmp_path) -> None:
    """DELETE /api/tasks/<id> — worker bearer must 401."""
    async with mcp_session(tmp_path) as admin:
        await _seed_task(admin, "wave3-task-2")
        worker = await admin.create_worker("task-attacker")
        r = admin.client.request(
            "DELETE",
            "/api/tasks/wave3-task-2",
            json={},
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 401, r.text


async def test_delete_task_rejects_no_auth(tmp_path) -> None:
    """DELETE /api/tasks/<id> — no auth must 401."""
    async with mcp_session(tmp_path) as admin:
        await _seed_task(admin, "wave3-task-3")
        r = admin.client.request(
            "DELETE", "/api/tasks/wave3-task-3", json={}
        )
        assert r.status_code == 401, r.text


# ── Section 3: task_notes — privilege check uses verify_token("manager")


async def _seed_manager_agent(agent_id: str) -> str:
    """INSERT an agents row with agent_role='manager' and return its
    token. Mirrors AdminClient.create_worker but flips the role."""
    import datetime
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.core import globals as g

    token = secrets.token_hex(16)
    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agents (token, agent_id, capabilities, "
            "created_at, status, working_directory, color, updated_at, "
            "agent_role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token, agent_id, "[]", now, "active", "/tmp", "#888",
                now, "manager",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    g.active_agents[token] = {
        "agent_id": agent_id,
        "status": "active",
        "created_at": now,
        "capabilities": [],
        "agent_role": "manager",
    }
    return token


async def _add_note(token: str, task_id: str, text: str) -> int:
    """Add a note via the MCP tool and return its note_id.

    Wave 6 PR 0 — ``dispatch_tool_call`` now returns
    :data:`agent_mcp.core.tool_result.ToolResult`; ``add_task_note``
    is the first tool to ship the migrated signature returning
    ``Ok(data={"note_id": ..., "task_id": ...}, ...)``. Pull the
    note_id from ``Ok.data`` rather than re-parsing the message.
    """
    from agent_mcp.tools.registry import (
        dispatch_tool_call, request_auth_token,
    )
    from agent_mcp.core.tool_result import Ok

    cv = request_auth_token.set(token)
    try:
        result = await dispatch_tool_call(
            "add_task_note",
            {"token": token, "task_id": task_id, "text": text},
        )
    finally:
        request_auth_token.reset(cv)
    assert isinstance(result, Ok), f"unexpected add_task_note result: {result!r}"
    note_id = result.data["note_id"]
    return int(note_id)


async def _edit_note(token: str, note_id: int, new_text: str) -> str:
    """Wave 6 PR 0 — ``edit_task_note`` is unmigrated; the bridge
    auto-wraps its legacy ``list[TextContent]`` return as
    ``Ok(message=...)``. Pull the message out so callers continue
    to see the same prose they did pre-Wave-6.
    """
    from agent_mcp.tools.registry import (
        dispatch_tool_call, request_auth_token,
    )
    from agent_mcp.core.tool_result import Ok

    cv = request_auth_token.set(token)
    try:
        result = await dispatch_tool_call(
            "edit_task_note",
            {"token": token, "note_id": note_id, "text": new_text},
        )
    finally:
        request_auth_token.reset(cv)
    if isinstance(result, Ok):
        return result.message or ""
    return ""


async def test_edit_task_note_admits_system_bearer(tmp_path) -> None:
    """The system bearer (legacy admin) must continue to admit
    non-author edits — the Wave 3 rewrite must preserve this."""
    async with mcp_session(tmp_path) as admin:
        await _seed_task(admin, "tn-task-1")
        worker = await admin.create_worker("note-author-1")
        note_id = await _add_note(worker.token, "tn-task-1", "v1")

        result = await _edit_note(admin.admin_token, note_id, "v2-by-admin")
        assert "updated" in result.lower(), result


async def test_edit_task_note_admits_manager_token(tmp_path) -> None:
    """Manager-role agent token must admit non-author edits — that's
    the Wave 3 generalisation: ``is_admin`` becomes
    ``verify_token(..., 'manager')``, so manager-tier callers can
    moderate worker notes."""
    async with mcp_session(tmp_path) as admin:
        await _seed_task(admin, "tn-task-2")
        worker = await admin.create_worker("note-author-2")
        note_id = await _add_note(worker.token, "tn-task-2", "v1")

        manager_token = await _seed_manager_agent("note-manager-2")
        result = await _edit_note(manager_token, note_id, "v2-by-manager")
        assert "updated" in result.lower(), (
            f"manager-role agent must edit non-author note; got {result!r}"
        )


async def test_edit_task_note_rejects_worker_non_author(tmp_path) -> None:
    """A worker-role agent token who is NOT the author must be
    rejected — the per-note ownership check is the only gate that
    stops cross-worker note mutation."""
    async with mcp_session(tmp_path) as admin:
        await _seed_task(admin, "tn-task-3")
        author = await admin.create_worker("note-author-3")
        note_id = await _add_note(author.token, "tn-task-3", "v1")

        intruder = await admin.create_worker("note-intruder-3")
        result = await _edit_note(intruder.token, note_id, "v2-by-intruder")
        # Failure case: error text from task_notes_db.edit_note (not the
        # decorator's "Unauthorized: …" wrap, because the tool itself is
        # @requires("any") — the per-note ownership check is what fails).
        assert "error" in result.lower() or "permitted" in result.lower(), (
            f"worker non-author must NOT be able to edit; got {result!r}"
        )


async def test_edit_task_note_admits_worker_author(tmp_path) -> None:
    """The original author (worker-role) must still be able to edit
    their own note — that's the pre-Wave-3 behaviour we must preserve."""
    async with mcp_session(tmp_path) as admin:
        await _seed_task(admin, "tn-task-4")
        author = await admin.create_worker("note-author-4")
        note_id = await _add_note(author.token, "tn-task-4", "v1")

        result = await _edit_note(author.token, note_id, "v2-by-self")
        assert "updated" in result.lower(), result


# ── Section 4: agent-prompt plumbing no longer references admin_token


def test_build_agent_prompt_signature_drops_admin_token() -> None:
    """``build_agent_prompt`` must no longer accept an ``admin_token``
    parameter. Wave 3 removes it from the prompt plumbing — the
    ``admin_agent`` template's ``{admin_token}`` substitution is gone
    too, so nothing downstream needs the value any more."""
    import inspect

    from agent_mcp.utils.prompt_templates import build_agent_prompt

    sig = inspect.signature(build_agent_prompt)
    assert "admin_token" not in sig.parameters, (
        f"build_agent_prompt must not accept admin_token any more; "
        f"signature: {sig}"
    )


def test_admin_agent_template_does_not_reference_admin_token() -> None:
    """The ``admin_agent`` template body must no longer substitute
    ``{admin_token}``. Wave 4 may delete the template entirely; Wave 3
    just removes the substitution so the parameter drop above is
    consistent."""
    from agent_mcp.utils.prompt_templates import PROMPT_TEMPLATES

    body = PROMPT_TEMPLATES.get("admin_agent", "")
    assert "{admin_token}" not in body, (
        f"admin_agent template must not reference {{admin_token}}; got: {body!r}"
    )


def test_generate_system_prompt_signature_drops_admin_token_runtime() -> None:
    """``generate_system_prompt`` must no longer accept an
    ``admin_token_runtime`` parameter. The Admin/Worker label now comes
    from the agent's ``agent_role`` column, not from a token-equality
    check."""
    import inspect

    from agent_mcp.utils.project_utils import generate_system_prompt

    sig = inspect.signature(generate_system_prompt)
    assert "admin_token_runtime" not in sig.parameters, (
        f"generate_system_prompt must not accept admin_token_runtime; "
        f"signature: {sig}"
    )


def test_agent_startup_template_does_not_mention_mcp_admin_token() -> None:
    """The agent startup script template must not advertise
    ``MCP_ADMIN_TOKEN`` — Wave 3 stops exporting it on spawn, and the
    comment block in the template is the public contract for what
    env-vars to expect."""
    from pathlib import Path

    template = Path(
        "agent_mcp/templates/agent_startup.sh"
    ).read_text()
    assert "MCP_ADMIN_TOKEN" not in template, (
        "Wave 3: agent startup template must not mention MCP_ADMIN_TOKEN"
    )


# retire-system-token Wave 3: ``test_config_json_writeback_uses_system_token_field``
# was deleted. The ``.agent/config.json`` writeback path that synthesised
# a ``system_token`` field is gone — ``init_agent_directory`` no longer
# writes a config.json, and the ``g.system_token`` global the test pinned
# has been deleted along with the rest of the system-bearer plumbing.
