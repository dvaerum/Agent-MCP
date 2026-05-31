"""Regression guards for the Tasks-page View dialog layout polish.

Background. Phase 7-UX1 (PR #49) added the View / Edit / Delete dialogs
on the Tasks page and gave them a first pass at shadcn-idiomatic
spacing. A follow-up Firefox MCP audit found four real bugs:

  1. **BLOCKING** — `sm:max-w-lg` from base `DialogContent` clobbered the
     `max-w-2xl` set in `ViewTaskDialog`. Every desktop dialog rendered
     at 512px instead of the intended 672px.
  2. Dialog overflowed viewport on huge descriptions (a 65k-char body
     pushed the dialog to 984px on a 1000px viewport).
  3. Unbreakable 65k-char tokens didn't wrap because `break-words`
     doesn't split single tokens.
  4. Title used `truncate` and silently dropped overflowing characters.

This file pins the structural fixes in `ViewTaskDialog` so a future
refactor of `DialogContent` can't silently re-introduce any of them.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard/components/dashboard/tasks-dashboard.tsx")


def _src() -> str:
    return DASHBOARD.read_text()


def test_view_dialog_uses_important_max_width_override() -> None:
    """`sm:!max-w-3xl` (with Tailwind `!` important) is the fix for the
    base `DialogContent`'s `sm:max-w-lg` winning the cascade. Without
    `!`, both classes have the same specificity and the later-declared
    base class wins — dialog squeezes to 512px on desktop."""
    src = _src()
    assert "sm:!max-w-3xl" in src, (
        "expected `sm:!max-w-3xl` on the View dialog's DialogContent "
        "to override the base DialogContent's `sm:max-w-lg`. Without "
        "the `!` (Tailwind important), the base class wins the "
        "cascade and the dialog renders at phone-narrow 512px on "
        "desktop. See bg-agent audit report (June 2026)."
    )


def test_view_dialog_caps_height_to_viewport() -> None:
    """`max-h-[90vh]` on the dialog + scrollable body region prevents
    the dialog from overflowing the viewport when the description is
    huge (we have one task with a 65k-char body in the wild)."""
    src = _src()
    assert "max-h-[90vh]" in src, (
        "expected `max-h-[90vh]` on the dialog content to cap dialog "
        "height. Without it, monster descriptions push the dialog "
        "past the viewport bottom."
    )
    assert "flex-1 min-h-0 overflow-y-auto" in src, (
        "expected the dialog body section to be `flex-1 min-h-0 "
        "overflow-y-auto` so it expands to fill the remaining space "
        "between the (flex-shrink-0) header and footer, and is the "
        "single scroll region."
    )


def test_view_dialog_handles_unbreakable_tokens() -> None:
    """`[overflow-wrap:anywhere]` on the description block forces
    unbreakable strings (e.g. 65k-char tokens) to wrap mid-token
    instead of overflowing horizontally."""
    src = _src()
    assert "[overflow-wrap:anywhere]" in src, (
        "expected `[overflow-wrap:anywhere]` on the description "
        "block so long unbroken strings wrap inside the block "
        "instead of forcing horizontal overflow of the dialog body."
    )


def test_view_dialog_title_wraps_instead_of_truncating() -> None:
    """Long titles should wrap (up to 3 lines via `line-clamp-3`)
    instead of silently dropping characters with `truncate`."""
    src = _src()
    assert "line-clamp-3" in src, (
        "expected `line-clamp-3` on the dialog title so long titles "
        "wrap to 3 lines instead of being silently truncated with "
        "`truncate` (which drops everything past the first line)."
    )
    # Negative: should NOT use `truncate` on the title anymore.
    # Truncate may legitimately appear elsewhere; this assertion is
    # scoped by looking for the title-specific span.
    title_block_start = src.find("<DialogTitle")
    title_block_end = src.find("</DialogTitle>", title_block_start)
    assert title_block_start >= 0 and title_block_end > title_block_start, (
        "couldn't locate the DialogTitle JSX block"
    )
    title_block = src[title_block_start:title_block_end]
    assert "truncate" not in title_block, (
        "the dialog title block should no longer use `truncate` — "
        "it silently drops overflowing characters. Use `break-words "
        "line-clamp-3` instead."
    )


def test_view_dialog_description_caps_height_inside_body() -> None:
    """Within the already-scrollable body, a huge description should
    scroll *inside* its own block (`max-h-[40vh] overflow-y-auto`)
    rather than ballooning the body. Otherwise the metadata footer
    falls off the bottom of the visible scroll region."""
    src = _src()
    # Look near the description region for the inner-scroll markers.
    description_block_idx = src.find("Description</Label>")
    assert description_block_idx >= 0, "couldn't locate description block"
    region = src[description_block_idx:description_block_idx + 600]
    assert "max-h-[40vh]" in region, (
        "expected `max-h-[40vh]` on the description block so monster "
        "descriptions scroll inside the description rather than "
        "balloon the dialog body and push other fields off-screen."
    )
