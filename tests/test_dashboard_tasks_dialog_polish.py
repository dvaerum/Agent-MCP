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

from tests.dashboard_sources import tasks_page_source


def _src() -> str:
    # Wave 5 (refactor/w5-tasks): the Tasks page was split into a page
    # module + a `tasks/` satellite directory. `ViewTaskDialog` (whose
    # layout these guards pin) now lives in
    # `tasks/view-task-dialog.tsx`, so read the page + its satellites as
    # one blob (mirrors the Messages/Agents split). See
    # tests/dashboard_sources.py.
    return tasks_page_source()


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


def test_view_dialog_description_no_inner_scroll() -> None:
    """The description block in the View dialog must NOT have its own
    scroll container — only the parent dialog body (`flex-1 min-h-0
    overflow-y-auto`) should scroll. Previously the description had
    `max-h-[40vh] overflow-y-auto` which created a nested scroll inside
    the dialog body — bad UX: users had to scroll two regions to read
    a long description plus metadata footer.

    The fix is to drop `max-h-[Nvh]` and `overflow-y-auto` from the
    description block so the description flows naturally and the whole
    dialog body scrolls as one. `[overflow-wrap:anywhere]` is kept so
    long unbreakable strings still wrap mid-token.
    """
    src = _src()
    description_block_idx = src.find("Description</Label>")
    assert description_block_idx >= 0, "couldn't locate description block"
    # Look only at the description's wrapping element + the <pre> body
    # (~400 chars after the label is plenty).
    region = src[description_block_idx:description_block_idx + 500]
    import re as _re
    assert not _re.search(r"max-h-\[\d+vh\]", region), (
        "expected NO `max-h-[Nvh]` constraint on the description block — "
        "the parent dialog body already scrolls (`max-h-[90vh]` + "
        "`flex-1 min-h-0 overflow-y-auto`), and nesting another scroll "
        "region inside it forces users to scroll twice. Drop the cap."
    )
    assert "overflow-y-auto" not in region, (
        "expected NO `overflow-y-auto` on the description block — only "
        "the parent dialog body should scroll. A nested scroll here was "
        "a UX regression (PR #54 polish over-corrected for long bodies)."
    )
    # Positive: the wrap helper must stay so 65k-char unbreakable tokens
    # still wrap mid-string instead of overflowing horizontally.
    assert "[overflow-wrap:anywhere]" in region, (
        "expected `[overflow-wrap:anywhere]` to be retained on the "
        "description block so long unbroken strings wrap mid-token "
        "(the dialog body's vertical scroll doesn't help horizontal "
        "overflow)."
    )
