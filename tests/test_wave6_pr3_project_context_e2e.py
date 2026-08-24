"""Wave 6 PR 3 — E2E coverage of the project_context_tools migration.

Pins the new Principal + ToolResult contract end-to-end for every
tool migrated in PR 3 (memory CRUD + context CRUD), through both
consumers of :func:`dispatch_tool_call`:

* The MCP wire path: bridge derives the Principal from ContextVars,
  the tool returns a typed :data:`ToolResult` variant, and
  :func:`render_as_text_content` converts it back to
  ``list[mcp_types.TextContent]`` for SSE/JSON-RPC clients.
* The REST adapter path: ``_dispatch_through_tool`` maps each
  :data:`ToolResult` variant to the HTTP status code the dashboard
  ApiClient expects (404 / 400 / 403 / 409 / 500 / 200 with data).

The companion tests in :mod:`tests.test_project_context_ownership`
(harness ``admin.call`` path) continue to cover the per-key
creator-ownership matrix end-to-end; this file focuses on the
additional Principal-aware shape every migrated tool now returns.

Why explicit Principal at the dispatch boundary
-----------------------------------------------
The PR 0 bridge derives the Principal from the existing ContextVars
when no ``principal=`` kwarg is supplied — old-style callers keep
working without modification. The tests below pass ``principal=``
explicitly so the assertion target is the typed contract, not the
ContextVar derivation order. Mixing both styles in one file pins
both the new path (explicit Principal) AND the bridge fallback
(no kwarg) so a future refactor that touches either still has a
fast-failing test guarding both.
"""

from __future__ import annotations

import pytest

from agent_mcp.app.rest_principal import RestPrincipal
from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import (
    Invalid,
    NotFound,
    Ok,
)
from tests.harness import dispatch_expecting_denial, make_principal, mcp_session

pytestmark = pytest.mark.asyncio


# ── Principal builders ───────────────────────────────────────────


def _operator_principal(
    user_id: str = "alice",
    project: str = "demo",
) -> Principal:
    """Construct an operator-tier Principal as the REST seam would.

    Mirrors what ``router/auth_middleware.py`` and
    ``app/main_app.py`` build after PR 0 — a cookie-authenticated
    dashboard caller. ``has_role("admin")`` admits this kind, which
    is the operator-tier gate every migrated tool uses for
    is-admin branching.
    """
    return make_principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=project,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _worker_principal(
    agent_id: str = "wkr-pr3",
    token: str = "wkr-pr3-token",
) -> Principal:
    """Construct an agent_bearer Principal for a worker-role agent.

    ``has_role("admin")`` returns False for this kind (workers
    don't bypass operator-only gates) and ``has_role("manager")``
    also returns False (the row's ``agent_role`` is ``"worker"``).
    """
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token=token,
    )


# ── view_project_context — Ok + admin-redaction matrix ────────────


async def test_view_project_context_returns_ok_with_entries(tmp_path) -> None:
    """A populated context view returns ``Ok(data={...}, message=...)``
    with the entries list in ``data``. Proves the structured-data
    payload reaches the caller alongside the legacy human-readable
    text in ``message``.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        # Seed one row via the REST memory create endpoint — same
        # path the dashboard uses.
        admin.post(
            "/api/memories",
            json={
                "context_key": "pr3.view.ok",
                "context_value": "hello",
            },
        )

        result = await dispatch_tool_call(
            "view_project_context",
            {"context_key": "pr3.view.ok"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert isinstance(result.data, dict)
    entries = result.data.get("entries") or []
    assert len(entries) == 1
    assert entries[0]["key"] == "pr3.view.ok"
    assert "pr3.view.ok" in (result.message or "")


def _seed_legacy_context_row(key: str, value) -> None:
    """Seed a project_context row RAW via the repository.

    Wave 11 (ADR-0016): the write path rejects config_* keys for every
    caller, so tests pinning the legacy read-side redaction on
    config-keyed rows (pre-cutover DB shapes) seed directly. The config
    branch of that machinery is deleted in the ADR-0016 follow-up PR.
    """
    import json as _json

    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.repositories import project_context_repository as _pc_repo

    conn = get_db_connection()
    try:
        _pc_repo.upsert(
            key,
            _json.dumps(value),
            None,
            description_provided=False,
            actor="admin",
            connection=conn.cursor(),
        )
        conn.commit()
    finally:
        conn.close()


async def test_view_project_context_admin_sees_secret_keys(tmp_path) -> None:
    """An operator-tier Principal sees every memory row (baseline).

    ADR-0017 (Wave 12 PR B): there is no content-based secret redaction on
    this surface — operators and workers alike see rows in full.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        _seed_legacy_context_row("config_pr3_secret_token", "shhh")

        result = await dispatch_tool_call(
            "view_project_context",
            {},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    keys = {e["key"] for e in (result.data.get("entries") or [])}
    assert "config_pr3_secret_token" in keys, (
        "operator-tier Principal must see secret-pattern keys"
    )
    assert "config_pr3_secret_token" in (result.message or "")


async def test_view_project_context_worker_sees_secret_keys(
    tmp_path,
) -> None:
    """ADR-0017 (Wave 12 PR B): a worker-tier Principal sees a
    secret-named memory row AS-IS. memory is shared project content,
    returned in full to any authorized reader — there is no content-based
    secret redaction on this surface any more."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        _seed_legacy_context_row("config_pr3_redact_token", "shhh")

        # The worker principal builder doesn't insert an agents row;
        # call directly with explicit Principal (the bridge's
        # bearer-resolve is irrelevant on this path).
        result = await dispatch_tool_call(
            "view_project_context",
            {},
            principal=_worker_principal(),
        )

    assert isinstance(result, Ok)
    keys = {e["key"] for e in (result.data.get("entries") or [])}
    assert "config_pr3_redact_token" in keys, (
        "ADR-0017: worker sees the memory row in full"
    )
    assert "config_pr3_redact_token" in (result.message or "")


async def test_view_project_context_anonymous_rejected(tmp_path) -> None:
    """A None Principal at the dispatch boundary is DENIED.

    Defence-in-depth: the per-tool gate matches the legacy
    ``@requires("any")`` rejection at the wire level. Wave 6 PR 6: with
    no bearer in arguments and no explicit principal, the dispatcher's
    arguments-token synthesis also returns None, so the tool's own gate
    fires.

    Phase 2 (Finding A): the gate is now
    ``@requires_capability("memories.view")`` on the impl, so the denial
    arrives as a raised ``AuthRejected`` rather than a returned
    ``PermissionDenied``. Same admission decision (the in-body pair was
    "authenticated AND memories.view", and every non-None Principal
    passed the identity half), same 403 / isError=True on the wire.
    """
    from agent_mcp.tools.registry import request_auth_token

    async with mcp_session(tmp_path):
        cv_token = request_auth_token.set(None)
        try:
            reason = await dispatch_expecting_denial(
                "view_project_context",
                {},
                principal=None,
            )
        finally:
            request_auth_token.reset(cv_token)

    assert reason.lower().startswith("unauthorized"), reason


# ── update_project_context — Ok / Invalid / PermissionDenied ──────


async def test_update_project_context_create_returns_ok_with_key(
    tmp_path,
) -> None:
    """A successful create returns ``Ok(data={"context_key": ...})``
    with the human-friendly success message. The ``data`` payload
    is what REST adapter consumers (the dashboard) read off the
    wire."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "update_project_context",
            {"context_key": "pr3.update.ok", "context_value": "value-1"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert result.data == {"context_key": "pr3.update.ok"}
    assert "pr3.update.ok" in (result.message or "")
    assert "success" in (result.message or "").lower()


async def test_update_project_context_missing_value_returns_invalid(
    tmp_path,
) -> None:
    """Single-update without ``context_value`` surfaces as
    :class:`Invalid` naming the offending field — exercised
    against the tool impl directly because the registered
    jsonschema rejects the call at the dispatcher boundary first
    (``"context_value is required"`` → :class:`ToolInputValidationError`).
    Hitting the impl's own guard proves the typed-return contract
    is in place for in-process callers that bypass the schema
    (REST routes that synthesize arguments, future Wave 7 split).
    """
    from agent_mcp.tools.project_context_tools import (
        update_project_context_tool_impl,
    )

    async with mcp_session(tmp_path):
        result = await update_project_context_tool_impl(
            {"context_key": "pr3.update.bad"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Invalid), f"expected Invalid, got {result!r}"
    # ``context_value`` is named because ``context_key`` is truthy in
    # the input; the field-naming heuristic in the tool picks the
    # first missing one.
    assert result.field == "context_value"


async def test_update_project_context_unserializable_value_returns_invalid(
    tmp_path,
) -> None:
    """A non-JSON-serialisable ``context_value`` returns
    :class:`Invalid` naming ``context_value``. Exercised against
    the impl directly because the registered jsonschema (`anyOf`
    over JSON-compatible primitives) rejects a custom class at
    the dispatcher boundary first; the impl's own ``json.dumps``
    try/except is the safety net for callers that bypass the
    schema."""
    from agent_mcp.tools.project_context_tools import (
        update_project_context_tool_impl,
    )

    class _NotJson:
        pass

    async with mcp_session(tmp_path):
        result = await update_project_context_tool_impl(
            {"context_key": "pr3.update.bad2", "context_value": _NotJson()},
            principal=_operator_principal(),
        )

    assert isinstance(result, Invalid)
    assert result.field == "context_value"
    assert "json" in result.message.lower()


async def test_update_project_context_config_key_rejected_for_worker(
    tmp_path,
) -> None:
    """A worker creating a ``config_*`` key surfaces as :class:`Invalid`
    with the config-only message.

    Worker-message clarity: the rejection is ``Invalid`` (an
    unprocessable input) rather than the Unauthorized-framed
    ``PermissionDenied`` — it steers the worker to a non-config_* key
    instead of an auth error it can't fix, and names no operator-only
    tool."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        # Stamp the worker's bearer + clear op_session via the
        # explicit Principal — the bridge isn't used since we pass
        # principal= directly.
        result = await dispatch_tool_call(
            "update_project_context",
            {
                "context_key": "config_pr3_worker_blocked",
                "context_value": "no",
            },
            principal=_worker_principal(),
        )

    assert isinstance(result, Invalid)
    assert "config_*" in result.message or "config_" in result.message


# ── bulk_update — Ok / PermissionDenied / Invalid ────────────────


async def test_bulk_update_project_context_returns_ok_with_summary(
    tmp_path,
) -> None:
    """A clean bulk update returns ``Ok`` whose ``data`` carries
    the per-entry summary lines."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "bulk_update_project_context",
            {
                "updates": [
                    {"context_key": "pr3.bulk.k1", "context_value": "v1"},
                    {"context_key": "pr3.bulk.k2", "context_value": "v2"},
                ],
            },
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert result.data["updates_attempted"] == 2
    summary = result.data.get("summary_lines") or []
    joined = "\n".join(summary)
    assert "pr3.bulk.k1" in joined
    assert "pr3.bulk.k2" in joined


async def test_bulk_update_empty_list_returns_invalid(tmp_path) -> None:
    """An empty ``updates`` list surfaces as :class:`Invalid`
    naming the ``updates`` field."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "bulk_update_project_context",
            {"updates": []},
            principal=_operator_principal(),
        )

    assert isinstance(result, Invalid)
    assert result.field == "updates"


# ── delete_project_context — Ok / NotFound / Invalid / PermissionDenied


async def test_delete_project_context_returns_ok_with_deleted_keys(
    tmp_path,
) -> None:
    """Successful delete returns ``Ok`` whose ``data`` lists the
    keys that were actually removed. Operators can see which
    requested keys actually existed without re-querying."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        admin.post(
            "/api/memories",
            json={
                "context_key": "pr3.del.ok",
                "context_value": "x",
            },
        )

        result = await dispatch_tool_call(
            "delete_project_context",
            {"context_key": "pr3.del.ok"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert result.data["deleted_count"] == 1
    assert result.data["deleted_keys"] == ["pr3.del.ok"]


async def test_delete_project_context_unknown_key_returns_not_found(
    tmp_path,
) -> None:
    """An unknown key surfaces as :class:`NotFound`. REST adapter
    maps this to 404, replacing the legacy text-matching path
    (``"none of the specified keys exist"`` regex)."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "delete_project_context",
            {"context_key": "pr3.del.missing"},
            principal=_operator_principal(),
        )

    assert isinstance(result, NotFound), (
        f"expected NotFound, got {result!r}"
    )
    assert result.resource == "project_context"
    assert "pr3.del.missing" in result.identifier


async def test_delete_project_context_no_keys_returns_invalid(
    tmp_path,
) -> None:
    """Empty key list surfaces as :class:`Invalid` — distinct from
    the unknown-key NotFound case so the caller can tell "you
    forgot to specify what to delete" from "what you asked to
    delete didn't exist"."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "delete_project_context",
            {},
            principal=_operator_principal(),
        )

    assert isinstance(result, Invalid)
    assert result.field == "context_key"


async def test_delete_project_context_critical_without_force_returns_invalid(
    tmp_path,
) -> None:
    """Critical-system key without ``force_delete=true`` surfaces
    as :class:`Invalid` naming the ``force_delete`` field. The
    distinction matters for the dashboard: it can re-prompt the
    operator with the force-flag, vs. surfacing a generic 400."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        # ADR-0016: config_* deletes are category-rejected on the context
        # path now, so the critical-key guard is exercised via a
        # non-config critical key.
        result = await dispatch_tool_call(
            "delete_project_context",
            {"context_key": "server_startup"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Invalid)
    assert result.field == "force_delete"


# ── validate_context_consistency — Ok with issues + warnings ──────


async def test_validate_context_consistency_empty_returns_ok(
    tmp_path,
) -> None:
    """An empty project returns ``Ok`` with zero issues + zero
    warnings — proves the early-return path threads the new
    return shape."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "validate_context_consistency",
            {},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    assert result.data == {"total_entries": 0, "issues": [], "warnings": []}


async def test_validate_context_consistency_reports_issues(
    tmp_path,
) -> None:
    """A seeded entry without a description surfaces as a warning
    in the structured payload, alongside the human-readable
    summary text. Proves the data + message pair carry parallel
    information for both REST and MCP consumers."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        admin.post(
            "/api/memories",
            json={
                "context_key": "pr3.validate.nodesc",
                "context_value": "ok",
            },
        )

        result = await dispatch_tool_call(
            "validate_context_consistency",
            {},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    assert result.data["total_entries"] >= 1
    warnings = result.data.get("warnings") or []
    assert any(
        "pr3.validate.nodesc" in w for w in warnings
    ), f"missing-description warning absent from {warnings!r}"


# ── backup_project_context — operator-only PermissionDenied ──────


async def test_backup_project_context_worker_returns_permission_denied(
    tmp_path,
) -> None:
    """An agent_bearer Principal (worker or manager) cannot back
    up. R21-F1 moved the gate onto ``@requires_capability`` (was an
    in-body ``_is_admin_principal`` check returning
    ``PermissionDenied``) — it now raises :class:`AuthRejected`; the
    REST adapter maps it to 403 with the reason in the envelope."""
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        with pytest.raises(AuthRejected) as excinfo:
            await dispatch_tool_call(
                "backup_project_context",
                {},
                principal=_worker_principal(),
            )

    assert "system.config.write" in str(excinfo.value)


async def test_backup_project_context_operator_returns_ok(tmp_path) -> None:
    """Operator-tier Principal succeeds and the ``data`` payload
    carries the backup path so callers can read it back.

    Seeds one row before backup because the pre-existing
    ``_analyze_context_health`` helper returns
    ``{"status": "no_data"}`` (no ``health_score`` key) for an
    empty project, and the backup tool's response-formatting
    block indexes ``health['health_score']`` unconditionally —
    out of PR 3's migration scope to fix.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        admin.post(
            "/api/memories",
            json={
                "context_key": "pr3.backup.seed",
                "context_value": "anything",
            },
        )

        result = await dispatch_tool_call(
            "backup_project_context",
            {"backup_name": "pr3-test-backup"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert result.data["backup_name"] == "pr3-test-backup"
    assert result.data["backup_path"].endswith("pr3-test-backup.json")
    assert result.data["total_entries"] >= 1


# ── Bridge fallback — old-style call works on migrated tools ──────


async def test_explicit_operator_principal_admits_config_write(
    tmp_path,
) -> None:
    """An explicit operator-session :class:`Principal` admits
    :func:`_is_admin_principal` and a config_* write succeeds — on the
    SETTINGS store (ADR-0016: the context path rejects the namespace
    for everyone; ``update_project_settings`` is the config surface).

    Wave 6 PR 6: the ContextVar bridge is gone — callers thread the
    Principal explicitly.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        op = _operator_principal(user_id="bridge-tester")
        result = await dispatch_tool_call(
            "update_project_settings",
            {
                "context_key": "config_pr3_bridge_op",
                "context_value": "via-bridge",
            },
            principal=op,
        )

    assert isinstance(result, Ok), (
        f"explicit operator must admit config_* write; "
        f"got {result!r}"
    )


# ── REST adapter parity — ToolResult → HTTP status code ──────────


async def test_rest_adapter_maps_not_found_to_404(tmp_path) -> None:
    """``_dispatch_through_tool`` maps :class:`NotFound` to 404
    with the resource + identifier in the envelope, replacing the
    legacy ``"none of the specified keys exist"`` regex path."""
    from agent_mcp.app._dispatch_helpers import _dispatch_through_tool

    async with mcp_session(tmp_path) as admin:  # noqa: F841 (lifespan)
        response = await _dispatch_through_tool(
            "delete_project_context",
            {"context_key": "pr3.rest.missing"},
            bearer_token=None,
            auth=RestPrincipal(kind="session", user={"username": "alice"}),
        )

    assert response.status_code == 404, response.body
    import json as _json
    body = _json.loads(response.body)
    assert body["error"] == "not_found"
    assert body["resource"] == "project_context"
    assert "pr3.rest.missing" in body["identifier"]


async def test_rest_adapter_maps_invalid_to_400_with_field(tmp_path) -> None:
    """``Invalid(field=...)`` from delete surfaces as 400 with
    the field name in the JSON body — same shape PR 0 demoed for
    ``add_task_note``."""
    from agent_mcp.app._dispatch_helpers import _dispatch_through_tool

    async with mcp_session(tmp_path) as admin:  # noqa: F841
        response = await _dispatch_through_tool(
            "delete_project_context",
            {},
            bearer_token=None,
            auth=RestPrincipal(kind="session", user={"username": "alice"}),
        )

    assert response.status_code == 400, response.body
    import json as _json
    body = _json.loads(response.body)
    assert body["error"] == "invalid"
    assert body["field"] == "context_key"


async def test_rest_adapter_maps_ok_data_to_200_with_payload(
    tmp_path,
) -> None:
    """A successful update routes through the REST adapter as
    200 with the ``data`` payload echoed back. Same shape that
    Wave 7 will let the dashboard rely on for typed responses."""
    from agent_mcp.app._dispatch_helpers import _dispatch_through_tool

    async with mcp_session(tmp_path) as admin:  # noqa: F841
        response = await _dispatch_through_tool(
            "update_project_context",
            {
                "context_key": "pr3.rest.ok",
                "context_value": "via-rest",
            },
            bearer_token=None,
            auth=RestPrincipal(kind="session", user={"username": "alice"}),
        )

    assert response.status_code == 200, response.body
    import json as _json
    body = _json.loads(response.body)
    assert body["success"] is True
    assert body["data"] == {"context_key": "pr3.rest.ok"}


# ── MCP wire path — TextContent rendering of ToolResult ───────────


async def test_mcp_wire_renders_ok_message_as_text_content(
    tmp_path,
) -> None:
    """The migrated tool's ``Ok(message=...)`` renders to a
    single TextContent block whose ``text`` is the message —
    proving the wire shape MCP clients see is unchanged from
    pre-migration. Goes through the registered MCP framework
    handler so ``mcp_call_tool_handler →
    render_as_text_content`` is exercised."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.assert_tool_succeeds(
            "update_project_context",
            {
                "context_key": "pr3.mcp.wire",
                "context_value": "txt",
            },
        )
        text = result[0].text
        assert "pr3.mcp.wire" in text
        assert "success" in text.lower()


async def test_mcp_wire_renders_permission_denied_for_worker(
    tmp_path,
) -> None:
    """A worker calling ``backup_project_context`` over the MCP
    wire receives ``isError=true`` + the ``Unauthorized: ...``
    text. Proves the :class:`PermissionDenied` variant renders
    on the wire the way historical "Unauthorized:" text did, so
    MCP clients that string-match for the prefix keep working."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("pr3-wire-wkr")
        # backup is visibility="operator" → the tools/list filter
        # hides it from workers, but a worker that bypasses the
        # filter and dispatches anyway hits the per-tool gate.
        await worker.assert_unauthorized("backup_project_context", {})
