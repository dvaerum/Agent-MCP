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

Wave 11 (ADR-0016): ``config_*`` toggles live in the ``project_settings``
store and the project_context write path rejects the namespace for
EVERYONE — so the wake-parity invariant now applies to the SETTINGS
write/delete surfaces (MCP ``update_project_settings`` /
``delete_project_settings``, REST ``PUT/DELETE /api/settings...``).
The parametrized matrix below is retargeted accordingly; the context
bulk-tool tests became rejection guards (a config toggle can no longer
be flipped through the context tools at all, so no wake may fire).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ── #1: RED-then-GREEN — the standalone bulk tool must wake loop ─────
# ── toggle waiters, matching its two write-surface siblings. ─────────


async def test_bulk_tool_config_toggle_rejected_and_fires_no_wake(
    tmp_path: Path,
) -> None:
    """Wave 11 (ADR-0016): a config toggle can no longer be flipped via
    ``bulk_update_project_context`` at all — the write is rejected for
    every caller, so NEITHER wake may fire (nothing changed)."""
    import agent_mcp.tools.project_context_tools as pct
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit, patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            result = await admin.call(
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
            text = result[0].text if result else ""
            assert admin._last_is_error, (
                f"bulk context write of config_* must be rejected: {text}"
            )
            assert not wake.called, (
                "a rejected config write must not wake waiters"
            )
            assert not emit.called, (
                "a rejected config write must not push tools/list_changed"
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


# ── #1 parametrized: the SAME wake set fires on every settings ───────
# ── write/delete surface for each toggle class (post-ADR-0016 the ────
# ── toggles live in project_settings; the context tools reject them). ─

WORKER_TOGGLE_KEY = "config_allow_worker_self_assign"
LOOP_TOGGLE_KEY = "config_auto_event_loop_global"


def _seed_setting_row(key: str, value) -> None:
    """Seed a ``project_settings`` row directly via the repository —
    deliberately NOT through ``update_project_settings`` — so the seed
    write can't itself trip the ``_emit_tools_list_changed`` /
    ``wake_all_for_flag_recheck`` mocks the shared parametrized test
    patches; only the surface call under test may make them fire."""
    import json as _json

    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.repositories import (
        project_settings_repository as _ps_repo,
    )

    conn = get_db_connection()
    try:
        _ps_repo.upsert(
            key,
            _json.dumps(value),
            "seed for wake parity test",
            description_provided=True,
            actor="r6-p-operator",
            connection=conn.cursor(),
        )
        conn.commit()
    finally:
        conn.close()


async def _single_update(admin, key: str, value) -> None:
    await admin.assert_tool_succeeds(
        "update_project_settings",
        {"context_key": key, "context_value": value},
    )


async def _update_via_rest(admin, key: str, value) -> None:
    """The REST PUT surface — dispatches the same gated
    ``update_project_settings`` tool (single enforcement path), so the
    wake set must match the MCP surface exactly (BL-R14-1)."""
    r = admin.request(
        "PUT",
        f"/api/settings/{key}",
        json={"context_value": value},
    )
    assert r.status_code == 200, r.text


async def _delete_via_tool(admin, key: str, value) -> None:
    """The MCP DELETE surface — a deleted toggle reverts to its
    default, so the delete must fire the same wake set as an update
    (the SEC-C / F5 invariant, carried to the settings store)."""
    _seed_setting_row(key, value)
    await admin.assert_tool_succeeds(
        "delete_project_settings",
        {"context_key": key},
    )


async def _delete_via_rest(admin, key: str, value) -> None:
    """The REST DELETE surface — the dashboard's delete path; routes
    through the gated ``delete_project_settings`` tool (pentest R1-F3
    invariant, carried to the settings store)."""
    _seed_setting_row(key, value)
    r = admin.request(
        "DELETE",
        f"/api/settings/{key}",
        json={},
    )
    assert r.status_code == 200, r.text


WRITE_SURFACES = [
    ("single_update", _single_update),
    ("update_via_rest", _update_via_rest),
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
    """Every settings write/delete surface must fire the SAME wake set
    for a given toggle class: worker-policy → tools/list_changed only;
    loop → wake_all_for_flag_recheck only."""
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


async def test_config_key_denial_is_typed_invalid() -> None:
    """``_check_write_authorization`` returns a typed :class:`Invalid` for
    a config_* key (worker-message clarity: NOT the Unauthorized-framed
    ``PermissionDenied``), pointing the caller at the settings store and
    naming no operator-only tool."""
    from agent_mcp.core.tool_result import Invalid
    from agent_mcp.tools import project_context_tools as pct

    denied = pct._check_write_authorization(
        connection=None,
        requesting_agent_id="worker-1",
        context_key="config_secret_thing",
        is_admin=False,
    )
    assert isinstance(denied, Invalid), (
        f"expected a typed Invalid, got {denied!r}"
    )
    # ADR-0016: the denial points the caller at the settings store …
    assert "project settings store" in denied.message
    # … but NOT at the operator-only update_project_settings tool.
    assert "update_project_settings" not in denied.message


async def test_creator_mismatch_wire_text_has_exactly_one_unauthorized_prefix(
    tmp_path: Path,
) -> None:
    """End-to-end: a worker denied for writing ANOTHER worker's key sees
    EXACTLY one ``"Unauthorized: "`` prefix on the MCP wire — pins the
    renderer-supplies-the-prefix contract this refactor depends on.

    (The vehicle is the creator-mismatch denial, which stays a typed
    :class:`PermissionDenied`; the config_* denial is now ``Invalid`` and
    no longer carries the Unauthorized prefix.)"""
    from tests.harness import _first_text

    async with mcp_session(tmp_path) as admin:
        worker_a = await admin.create_worker("wkr-authz-r6-A")
        worker_b = await admin.create_worker("wkr-authz-r6-B")
        # A creates the key; B (a different creator) is denied on write.
        await worker_a.call(
            "update_project_context",
            {"context_key": "owned_by_a", "context_value": "v"},
        )
        result = await worker_b.call(
            "update_project_context",
            {"context_key": "owned_by_a", "context_value": "should not land"},
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
