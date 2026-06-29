"""PR-W1b — InstructionsContributor registry tests.

Spec (architecture review 2026-06-05, Finding #3):

  The `_patched_create_initialization_options` monkeypatch in
  `agent_mcp/app/main_app.py` currently embeds two inline contributors
  (alias deprecation warning + wake-loop bootstrap) that each call into
  their own gating logic and append to `serverInfo.instructions`. Adding
  a third contributor today requires reaching back into the patch and
  growing the conditional chain.

  The refactor introduces an `InstructionsContributor` registry in
  `agent_mcp.app.instructions_contributors`:

      InstructionsContributor = Callable[[InitContext], str | None]

      register(name, fn)
      render_all(ctx) -> str  # concatenated outputs in registration order

  The monkeypatch becomes a one-liner:

      base.instructions = (base.instructions or "") + render_all(ctx)

  And the two existing contributors register themselves at module
  import time (alias-warning, wake-loop).

Test plan:
  A. register a fake contributor; `render_all` includes its output.
  B. register two contributors; both appear in registration order.
  C. contributor returning None is skipped (not even an empty separator
     gap leaks through).
  D. integration — an `initialize` request through the real harness
     surfaces any extra contributor's text inside
     `result.instructions`.

All four tests must FAIL on `origin/main` (the registry module doesn't
exist yet) and PASS after the GREEN commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _fresh_registry():
    """Import-reset the contributors module so test-registered
    contributors don't bleed into other tests.

    The module keeps a module-level list (`_contributors`); we snapshot
    it on entry and restore on exit. Cheaper + more deterministic than
    `importlib.reload`, which would also drop the production
    contributors registered at import.
    """
    from agent_mcp.app import instructions_contributors as ic

    snapshot = list(ic._contributors)

    def _restore() -> None:
        ic._contributors.clear()
        ic._contributors.extend(snapshot)

    return ic, _restore


def _empty_ctx():
    """Build an InitContext with no bearer + no alias info.

    Production contributors (alias-warning, wake-loop) gate themselves
    off when both fields are absent, so an empty context guarantees
    they contribute nothing — leaving the test-registered fake as the
    only output. Keeps the assertions tight.
    """
    from agent_mcp.app.instructions_contributors import InitContext

    return InitContext(bearer=None, alias_info=None)


def test_register_and_render_single_contributor() -> None:
    """Test A: a registered contributor's text appears in render_all."""
    ic, restore = _fresh_registry()
    try:
        ic.register(
            "test-fake",
            lambda ctx: "FAKE TEST INSTRUCTION",
        )
        rendered = ic.render_all(_empty_ctx())
        assert "FAKE TEST INSTRUCTION" in rendered
    finally:
        restore()


def test_register_two_contributors_preserves_order() -> None:
    """Test B: two contributors render in registration order."""
    ic, restore = _fresh_registry()
    try:
        ic.register("test-first", lambda ctx: "FIRST_BLOCK")
        ic.register("test-second", lambda ctx: "SECOND_BLOCK")
        rendered = ic.render_all(_empty_ctx())
        first = rendered.find("FIRST_BLOCK")
        second = rendered.find("SECOND_BLOCK")
        assert first >= 0 and second >= 0, (
            f"both blocks must appear; got {rendered!r}"
        )
        assert first < second, (
            f"FIRST must come before SECOND; got {rendered!r}"
        )
    finally:
        restore()


def test_contributor_returning_none_is_skipped() -> None:
    """Test C: a contributor returning None contributes nothing."""
    ic, restore = _fresh_registry()
    try:
        ic.register("test-silent", lambda ctx: None)
        ic.register("test-vocal", lambda ctx: "ONLY_VOCAL_TEXT")
        rendered = ic.render_all(_empty_ctx())
        assert "ONLY_VOCAL_TEXT" in rendered
        # No empty-string surrogate for the silent contributor.
        assert "None" not in rendered
    finally:
        restore()


def test_byte_identical_alias_warning_output() -> None:
    """Regression: the alias-warning contributor's output must be
    byte-identical to the pre-refactor inline construction.

    Before PR-W1b the warning was produced by
    ``_build_alias_warning(name, expires)`` and appended directly. The
    contributor must call the same builder with the same args so the
    wire text doesn't drift — clients comparing two consecutive
    initialize responses (e.g. for caching) must see no diff.
    """
    from agent_mcp.app.instructions_contributors import (
        InitContext,
        _alias_warning_contributor,
    )
    from agent_mcp.app.main_app import _build_alias_warning

    ctx = InitContext(
        bearer=None,
        alias_info=("legacy-name", "2026-07-15T00:00:00Z"),
    )
    contributor_output = _alias_warning_contributor(ctx)
    direct_output = _build_alias_warning(
        "legacy-name", "2026-07-15T00:00:00Z"
    )
    assert contributor_output == direct_output, (
        "alias-warning contributor output drifted from "
        "_build_alias_warning; clients diffing initialize responses "
        "will see a behaviour change."
    )


def test_byte_identical_wake_loop_output() -> None:
    """Regression: the wake-loop contributor must return exactly
    ``WAKE_LOOP_INSTRUCTIONS`` (no leading/trailing modification)
    when the gate passes. The text is also surfaced via the
    ``agent-mcp-enter-event-loop`` MCP prompt and through the
    initialize injection; both paths must serve the same bytes.

    We force the gate ON by monkeypatching the eligibility check; the
    point of this test is to lock the contributor's output, not to
    re-test the gating logic (covered in test_event_coord_toggles.py).
    """
    from agent_mcp.app import instructions_contributors as ic
    from agent_mcp.app.event_loop_instructions import WAKE_LOOP_INSTRUCTIONS
    from agent_mcp.core.principal import Principal

    # Wave 6 PR 6: the contributor reads ``principal.can_wake_loop``
    # directly. Build a Principal with the bit on and pass it via
    # the ``InitContext``; no monkeypatching needed.
    p = Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id="alice",
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=True,
        source_token="anything",
    )
    ctx = ic.InitContext(bearer="anything", alias_info=None, principal=p)
    assert ic._wake_loop_contributor(ctx) == WAKE_LOOP_INSTRUCTIONS


@pytest.mark.asyncio
async def test_initialize_surfaces_registered_contributor(
    tmp_path: Path,
) -> None:
    """Test D: an `initialize` request through the real harness picks
    up a contributor registered via the public API.

    Mirrors the alias-warning E2E test (`tests/test_alias_warning.py`).
    The registered fake contributor returns a distinctive string that
    must appear in `result.instructions` of the initialize response.
    """
    from tests.harness import mcp_session
    from tests.test_alias_warning import _extract_initialize_result

    ic, restore = _fresh_registry()
    try:
        ic.register(
            "test-e2e",
            lambda ctx: "E2E_CONTRIBUTOR_MARKER",
        )

        async with mcp_session(tmp_path) as admin:
            r = admin.client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {admin.admin_token}",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
            )
            assert r.status_code == 200, r.text
            result = _extract_initialize_result(r.text)
            instructions = result.get("instructions") or ""
            assert "E2E_CONTRIBUTOR_MARKER" in instructions, (
                f"contributor output missing from initialize response; "
                f"got instructions={instructions!r}"
            )
    finally:
        restore()
