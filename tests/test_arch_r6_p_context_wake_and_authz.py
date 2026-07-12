"""arch-r6 work-item P: two ``project_context_tools.py`` findings.

#1 — LATENT BUG: the bulk write-wake fan-out drifted. The single-key
write path funnels every post-write wake through ONE seam,
``emit_context_write_wakes`` — worker-policy toggle →
``tools/list_changed``, loop toggle → ``wake_all_for_flag_recheck``.
The two bulk write surfaces hand-rolled the same fan-out and drifted:
``_handle_bulk_context_update`` (the queued path, reached via
``update_project_context`` with a list of updates) fired BOTH halves;
the standalone ``bulk_update_project_context`` tool fired ONLY the
worker-policy half. Flipping ``config_auto_event_loop_global`` through
the standalone bulk tool left in-flight ``wait_for_events`` waiters
hanging — the REST-vs-MCP notify-parity class BL-R14-1 (see
``emit_context_write_wakes``'s docstring), silently reintroduced on
the third write surface.

The fix adds ``emit_context_write_wakes_bulk`` next to the single-key
seam and routes BOTH bulk paths through it.

SEC-C / F5 (round-6 wake-parity gap follow-up) — ``delete_project_context_tool_impl``
was the last project_context write/delete surface that never routed
through the shared wake seam at all: deleting
``config_auto_event_loop_global`` reverted the flag to its default
without calling ``wake_all_for_flag_recheck``, and deleting a
``config_allow_worker_*`` key reverted worker tool visibility without
pushing ``notifications/tools/list_changed``. The fix routes the
delete path through ``emit_context_write_wakes_bulk`` (fired only
after the DB transaction commits, matching every other surface's
emit-after-commit placement) and ``delete_via_tool`` is added to
``WRITE_SURFACES`` below so the parametrized invariant becomes "every
project_context write AND delete surface fires the matching wake".

pentest R1-F3 (class-sweep MISS follow-up, this PR) — SEC-C's commit
claimed "REST /api/memories fires it" but only checked REST POST/PUT;
the REST **DELETE** handler (``delete_memory_api_route`` in
``app/routers/memories.py``) writes the DB directly via
``session.delete(row)`` + ``session.commit()`` and — unlike its POST
and PUT siblings, and unlike the MCP ``delete_project_context`` tool
this SEC-C commit fixed — never called ``emit_context_write_wakes``.
It is the dashboard's PRIMARY delete path. The fix adds the missing
call right after ``session.commit()``, and ``delete_via_rest`` is
added to ``WRITE_SURFACES`` below so the parametrized invariant now
covers every REST AND MCP write/delete surface.

#3 — leaky/dup: the "Unauthorized: " prefix round trip. The MCP wire
renderer (``core/tool_result.py::render_as_text_content``) already
prefixes every ``PermissionDenied`` with ``"Unauthorized: "``,  so the
5 call sites that stripped a hard-coded ``"Unauthorized: "`` prefix off
``_check_write_authorization``'s return value before re-wrapping it in
``PermissionDenied`` were performing a dead round trip. The fix makes
``_check_write_authorization`` (and the two error factories it calls)
return the typed ``PermissionDenied`` directly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ── #1: RED-then-GREEN — the standalone bulk tool must wake loop ─────
# ── toggle waiters, matching its two write-surface siblings. ─────────


async def test_bulk_tool_loop_toggle_wakes_waiters(tmp_path: Path) -> None:
    """RED before the fix: flipping ``config_auto_event_loop_global``
    via the STANDALONE ``bulk_update_project_context`` tool must call
    ``wake_all_for_flag_recheck`` — on main it silently did not, so an
    in-flight ``wait_for_events`` caller would hang past a global
    loop-off flip made through this specific surface."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            await admin.assert_tool_succeeds(
                "bulk_update_project_context",
                {
                    "updates": [
                        {
                            "context_key": "config_auto_event_loop_global",
                            "context_value": False,
                        },
                    ]
                },
            )
            assert wake.called, (
                "bulk_update_project_context flipping "
                "config_auto_event_loop_global must call "
                "wake_all_for_flag_recheck; it was never called"
            )


async def test_bulk_tool_worker_toggle_still_pushes_tools_list(
    tmp_path: Path,
) -> None:
    """Parity guard: the standalone bulk tool's pre-existing
    worker-policy push must survive the fix (don't regress the half
    that already worked)."""
    import agent_mcp.tools.project_context_tools as pct

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit:
            await admin.assert_tool_succeeds(
                "bulk_update_project_context",
                {
                    "updates": [
                        {
                            "context_key": "config_allow_worker_self_assign",
                            "context_value": True,
                        },
                    ]
                },
            )
            assert emit.called, (
                "bulk_update_project_context flipping a worker-policy "
                "toggle must still push tools/list_changed"
            )


async def test_bulk_tool_unrelated_write_fires_neither_wake(
    tmp_path: Path,
) -> None:
    """Regression guard: a plain (non-config) bulk write must fire
    NEITHER wake."""
    import agent_mcp.tools.project_context_tools as pct
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit, patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            await admin.assert_tool_succeeds(
                "bulk_update_project_context",
                {
                    "updates": [
                        {"context_key": "team_motto", "context_value": "ship it"},
                    ]
                },
            )
            assert not emit.called, (
                f"unrelated bulk write must not push tools/list_changed; "
                f"got: {emit.call_args_list}"
            )
            assert not wake.called, (
                f"unrelated bulk write must not wake waiters; "
                f"got: {wake.call_args_list}"
            )


# ── #1 parametrized: the SAME wake set fires on all three write ──────
# ── surfaces for each toggle class. ───────────────────────────────────

WORKER_TOGGLE_KEY = "config_allow_worker_self_assign"
LOOP_TOGGLE_KEY = "config_auto_event_loop_global"


async def _single_update(admin, key: str, value) -> None:
    await admin.assert_tool_succeeds(
        "update_project_context",
        {"context_key": key, "context_value": value},
    )


async def _bulk_via_update(admin, key: str, value) -> None:
    """The queued bulk path (``_handle_bulk_context_update``) —
    ``update_project_context_tool_impl`` called with an ``updates``
    list instead of a single ``context_key``/``context_value`` pair.

    The registered MCP tool schema for ``update_project_context``
    declares ``context_key``/``context_value`` required and
    ``additionalProperties: False``, so this shape can't reach the
    tool through the jsonschema-validated dispatcher
    (``admin.call``/``assert_tool_succeeds``) — it calls the impl
    function directly, same pattern as
    ``tests/test_uow_project_context.py``'s bulk-atomicity tests.
    """
    from agent_mcp.core.tool_result import Ok
    from agent_mcp.tools.project_context_tools import (
        update_project_context_tool_impl,
    )
    from tests.harness import make_principal

    operator = make_principal(
        kind="operator_session",
        user_id="r6-p-operator",
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )
    result = await update_project_context_tool_impl(
        {"updates": [{"context_key": key, "context_value": value}]},
        principal=operator,
    )
    assert isinstance(result, Ok), f"expected Ok, got {result!r}"


async def _bulk_via_bulk_tool(admin, key: str, value) -> None:
    """The standalone inline bulk path — the one this PR fixes."""
    await admin.assert_tool_succeeds(
        "bulk_update_project_context",
        {"updates": [{"context_key": key, "context_value": value}]},
    )


async def _delete_via_tool(admin, key: str, value) -> None:
    """The DELETE surface (SEC-C / F5) — before this fix
    ``delete_project_context_tool_impl`` was the ONLY project_context
    write/delete surface that never routed through
    ``emit_context_write_wakes``/``emit_context_write_wakes_bulk``, so
    deleting a toggle key silently reverted it to its default WITHOUT
    firing the matching wake.

    Seeds the row directly via the ORM — deliberately NOT through
    ``update_project_context`` — so the seed write can't itself trip
    the ``_emit_tools_list_changed``/``wake_all_for_flag_recheck``
    mocks the shared parametrized test patches; only the delete call
    below is allowed to make them fire.

    ``config_*`` keys also match the tool's "critical system key"
    guard (any key starting with ``config_`` needs
    ``force_delete=True`` — see ``delete_project_context_tool_impl``'s
    ``critical_keys`` matching), so the delete call passes it
    unconditionally; both toggle keys under test are ``config_*``.
    """
    import datetime as _dt
    import json as _json

    from agent_mcp.db.engine import SessionLocal
    from agent_mcp.db.models import ProjectContext

    now = _dt.datetime.now().isoformat()
    sess = SessionLocal()
    try:
        sess.add(
            ProjectContext(
                context_key=key,
                value=_json.dumps(value),
                created_at=now,
                created_by="r6-p-operator",
                updated_at=now,
                updated_by="r6-p-operator",
                description="seed for delete-wake parity test",
            )
        )
        sess.commit()
    finally:
        sess.close()

    await admin.assert_tool_succeeds(
        "delete_project_context",
        {"context_key": key, "force_delete": True},
    )


async def _delete_via_rest(admin, key: str, value) -> None:
    """The REST DELETE surface (pentest R1-F3) — the dashboard's
    PRIMARY delete path, and the one this PR fixes.
    ``delete_memory_api_route`` committed the row delete directly via
    SQLAlchemy and returned WITHOUT ever calling
    ``emit_context_write_wakes``/``emit_context_write_wakes_bulk`` —
    the last unwaked project_context write/delete surface after the
    SEC-C fix closed the MCP delete tool's version of the same gap.

    Seeds the row directly via the ORM — deliberately NOT through
    ``update_project_context`` — so the seed write can't itself trip
    the ``_emit_tools_list_changed``/``wake_all_for_flag_recheck``
    mocks the shared parametrized test patches; only the REST DELETE
    call below is allowed to make them fire.

    R9-F2: the REST DELETE handler now routes through the gated
    ``delete_project_context`` tool, which treats every ``config_*`` key
    as a critical system key requiring ``force_delete=true`` (both toggle
    keys under test are ``config_*``). The body now carries
    ``force_delete: true`` — the same value the ``_delete_via_tool`` path
    passes — so the delete lands and the wake fires.
    """
    import datetime as _dt
    import json as _json

    from agent_mcp.db.engine import SessionLocal
    from agent_mcp.db.models import ProjectContext

    now = _dt.datetime.now().isoformat()
    sess = SessionLocal()
    try:
        sess.add(
            ProjectContext(
                context_key=key,
                value=_json.dumps(value),
                created_at=now,
                created_by="r6-p-operator",
                updated_at=now,
                updated_by="r6-p-operator",
                description="seed for REST delete-wake parity test",
            )
        )
        sess.commit()
    finally:
        sess.close()

    r = admin.client.request(
        "DELETE",
        f"/api/memories/{key}",
        json={"token": admin.admin_token, "force_delete": True},
    )
    assert r.status_code == 200, r.text


WRITE_SURFACES = [
    ("single_update", _single_update),
    ("bulk_via_update", _bulk_via_update),
    ("bulk_via_bulk_tool", _bulk_via_bulk_tool),
    ("delete_via_tool", _delete_via_tool),
    ("delete_via_rest", _delete_via_rest),
]

TOGGLES = [
    ("worker_policy_toggle", WORKER_TOGGLE_KEY, True, False),
    ("loop_toggle", LOOP_TOGGLE_KEY, False, True),
]


@pytest.mark.parametrize(
    "surface_name,write_fn", WRITE_SURFACES, ids=[s[0] for s in WRITE_SURFACES]
)
@pytest.mark.parametrize(
    "toggle_name,toggle_key,expect_emit,expect_wake",
    TOGGLES,
    ids=[t[0] for t in TOGGLES],
)
async def test_write_surfaces_fire_same_wake_set(
    tmp_path: Path,
    surface_name: str,
    write_fn,
    toggle_name: str,
    toggle_key: str,
    expect_emit: bool,
    expect_wake: bool,
) -> None:
    """Every one of the three write surfaces must fire the SAME wake
    set for a given toggle class: worker-policy → tools/list_changed
    only; loop → wake_all_for_flag_recheck only."""
    import agent_mcp.tools.project_context_tools as pct
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit, patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            await write_fn(admin, toggle_key, False)

            assert emit.called is expect_emit, (
                f"[{surface_name}/{toggle_name}] tools/list_changed "
                f"called={emit.called}, expected {expect_emit}"
            )
            assert wake.called is expect_wake, (
                f"[{surface_name}/{toggle_name}] wake_all_for_flag_recheck "
                f"called={wake.called}, expected {expect_wake}"
            )


# ── #3: typed authz denial, no double-prefix ──────────────────────────


async def test_config_key_denial_is_typed_permission_denied() -> None:
    """``_check_write_authorization`` returns a :class:`PermissionDenied`
    directly (not a pre-formatted ``"Unauthorized: ..."`` string) and
    the reason text carries NO leading ``"Unauthorized: "`` — the wire
    renderer supplies that prefix exactly once."""
    from agent_mcp.core.tool_result import PermissionDenied
    from agent_mcp.tools import project_context_tools as pct

    denied = pct._check_write_authorization(
        connection=None,
        requesting_agent_id="worker-1",
        context_key="config_secret_thing",
        is_admin=False,
    )
    assert isinstance(denied, PermissionDenied), (
        f"expected a typed PermissionDenied, got {denied!r}"
    )
    assert not denied.reason.lower().startswith("unauthorized"), (
        f"reason must not carry its own Unauthorized prefix (the "
        f"renderer adds it once); got {denied.reason!r}"
    )
    assert "config_* keys are admin-only" in denied.reason


async def test_config_key_wire_text_has_exactly_one_unauthorized_prefix(
    tmp_path: Path,
) -> None:
    """End-to-end: a worker denied for writing a config_* key sees
    EXACTLY one ``"Unauthorized: "`` prefix on the MCP wire — pins the
    renderer-supplies-the-prefix contract this refactor depends on."""
    from tests.harness import _first_text

    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-authz-r6")
        result = await wkr.call(
            "update_project_context",
            {
                "context_key": "config_secret_r6",
                "context_value": "should not land",
            },
        )
        text = _first_text(result)
        assert text.startswith("Unauthorized: "), (
            f"expected the wire text to start with 'Unauthorized: '; "
            f"got {text!r}"
        )
        # Exactly one occurrence — a dead round trip would double-stamp
        # it (e.g. "Unauthorized: Unauthorized: ...").
        assert text.count("Unauthorized:") == 1, (
            f"expected exactly one 'Unauthorized:' prefix; got {text!r}"
        )
