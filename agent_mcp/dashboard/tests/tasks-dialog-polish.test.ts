/**
 * Regression guards for the Tasks-page View dialog layout polish.
 *
 * Background. Phase 7-UX1 (PR #49) added the View / Edit / Delete dialogs
 * on the Tasks page and gave them a first pass at shadcn-idiomatic
 * spacing. A follow-up Firefox MCP audit found four real bugs:
 *
 *   1. **BLOCKING** — `sm:max-w-lg` from base `DialogContent` clobbered the
 *      `max-w-2xl` set in `ViewTaskDialog`. Every desktop dialog rendered
 *      at 512px instead of the intended 672px.
 *   2. Dialog overflowed viewport on huge descriptions (a 65k-char body
 *      pushed the dialog to 984px on a 1000px viewport).
 *   3. Unbreakable 65k-char tokens didn't wrap because `break-words`
 *      doesn't split single tokens.
 *   4. Title used `truncate` and silently dropped overflowing characters.
 *
 * This file pins the structural fixes in `ViewTaskDialog` so a future
 * refactor of `DialogContent` can't silently re-introduce any of them.
 *
 * Ported from tests/test_dashboard_tasks_dialog_polish.py (Python
 * source tree retired).
 */

import { describe, expect, it } from "vitest"
import { tasksPageSource } from "./support/tasks-source"

function src(): string {
  // Wave 5 (refactor/w5-tasks): the Tasks page was split into a page
  // module + a `tasks/` satellite directory. `ViewTaskDialog` (whose
  // layout these guards pin) now lives in `tasks/view-task-dialog.tsx`,
  // so read the page + its satellites as one blob (mirrors the
  // Messages/Agents split). See tests/support/tasks-source.ts.
  return tasksPageSource()
}

describe("View dialog width override", () => {
  it("uses sm:!max-w-3xl (Tailwind important) to beat the base sm:max-w-lg", () => {
    // `sm:!max-w-3xl` (with Tailwind `!` important) is the fix for the
    // base `DialogContent`'s `sm:max-w-lg` winning the cascade. Without
    // `!`, both classes have the same specificity and the later-declared
    // base class wins — dialog squeezes to 512px on desktop.
    expect(
      src().includes("sm:!max-w-3xl"),
      "expected `sm:!max-w-3xl` on the View dialog's DialogContent " +
        "to override the base DialogContent's `sm:max-w-lg`. Without " +
        "the `!` (Tailwind important), the base class wins the " +
        "cascade and the dialog renders at phone-narrow 512px on " +
        "desktop. See bg-agent audit report (June 2026).",
    ).toBe(true)
  })
})

describe("View dialog viewport height cap", () => {
  it("caps height to 90dvh with a single flex-1 min-h-0 overflow-y-auto scroll body", () => {
    // `max-h-[90dvh]` on the dialog + scrollable body region prevents
    // the dialog from overflowing the viewport when the description is
    // huge (we have one task with a 65k-char body in the wild).
    //
    // `dvh`, not `vh`: `vh` is computed against the largest possible
    // viewport, not the one actually visible once a mobile browser's
    // chrome (address bar, bottom bar) collapses — see
    // docs/learnings/dashboard-dialog-mobile-clipping.md.
    const s = src()
    expect(
      s.includes("max-h-[90dvh]"),
      "expected `max-h-[90dvh]` on the dialog content to cap dialog " +
        "height. Without it, monster descriptions push the dialog " +
        "past the viewport bottom.",
    ).toBe(true)
    expect(
      s.includes("flex-1 min-h-0 overflow-y-auto"),
      "expected the dialog body section to be `flex-1 min-h-0 " +
        "overflow-y-auto` so it expands to fill the remaining space " +
        "between the (flex-shrink-0) header and footer, and is the " +
        "single scroll region.",
    ).toBe(true)
  })
})

describe("View dialog unbreakable-token wrapping", () => {
  it("forces mid-token wrapping via [overflow-wrap:anywhere]", () => {
    // `[overflow-wrap:anywhere]` on the description block forces
    // unbreakable strings (e.g. 65k-char tokens) to wrap mid-token
    // instead of overflowing horizontally.
    expect(
      src().includes("[overflow-wrap:anywhere]"),
      "expected `[overflow-wrap:anywhere]` on the description " +
        "block so long unbroken strings wrap inside the block " +
        "instead of forcing horizontal overflow of the dialog body.",
    ).toBe(true)
  })
})

describe("View dialog title wraps instead of truncating", () => {
  it("uses line-clamp-3 and drops truncate on the DialogTitle block", () => {
    // Long titles should wrap (up to 3 lines via `line-clamp-3`) instead
    // of silently dropping characters with `truncate`.
    const s = src()
    expect(
      s.includes("line-clamp-3"),
      "expected `line-clamp-3` on the dialog title so long titles " +
        "wrap to 3 lines instead of being silently truncated with " +
        "`truncate` (which drops everything past the first line).",
    ).toBe(true)

    // Negative: should NOT use `truncate` on the title anymore.
    // Truncate may legitimately appear elsewhere; this assertion is
    // scoped by looking for the title-specific span.
    const titleBlockStart = s.indexOf("<DialogTitle")
    const titleBlockEnd = s.indexOf("</DialogTitle>", titleBlockStart)
    expect(
      titleBlockStart >= 0 && titleBlockEnd > titleBlockStart,
      "couldn't locate the DialogTitle JSX block",
    ).toBe(true)
    const titleBlock = s.slice(titleBlockStart, titleBlockEnd)
    expect(
      titleBlock.includes("truncate"),
      "the dialog title block should no longer use `truncate` — " +
        "it silently drops overflowing characters. Use `break-words " +
        "line-clamp-3` instead.",
    ).toBe(false)
  })
})

describe("View dialog description has no nested scroll region", () => {
  it("drops max-h-[Nvh] and overflow-y-auto from the description block, keeps overflow-wrap", () => {
    // The description block in the View dialog must NOT have its own
    // scroll container — only the parent dialog body (`flex-1 min-h-0
    // overflow-y-auto`) should scroll. Previously the description had
    // `max-h-[40vh] overflow-y-auto` which created a nested scroll inside
    // the dialog body — bad UX: users had to scroll two regions to read
    // a long description plus metadata footer.
    //
    // The fix is to drop `max-h-[Nvh]` and `overflow-y-auto` from the
    // description block so the description flows naturally and the whole
    // dialog body scrolls as one. `[overflow-wrap:anywhere]` is kept so
    // long unbreakable strings still wrap mid-token.
    const s = src()
    const descriptionBlockIdx = s.indexOf("Description</Label>")
    expect(descriptionBlockIdx >= 0, "couldn't locate description block").toBe(true)
    // Look only at the description's wrapping element + the <pre> body
    // (~500 chars after the label is plenty).
    const region = s.slice(descriptionBlockIdx, descriptionBlockIdx + 500)
    expect(
      /max-h-\[\d+d?vh\]/.test(region),
      "expected NO `max-h-[Nvh]` constraint on the description block — " +
        "the parent dialog body already scrolls (`max-h-[90dvh]` + " +
        "`flex-1 min-h-0 overflow-y-auto`), and nesting another scroll " +
        "region inside it forces users to scroll twice. Drop the cap.",
    ).toBe(false)
    expect(
      region.includes("overflow-y-auto"),
      "expected NO `overflow-y-auto` on the description block — only " +
        "the parent dialog body should scroll. A nested scroll here was " +
        "a UX regression (PR #54 polish over-corrected for long bodies).",
    ).toBe(false)
    // Positive: the wrap helper must stay so 65k-char unbreakable tokens
    // still wrap mid-string instead of overflowing horizontally.
    expect(
      region.includes("[overflow-wrap:anywhere]"),
      "expected `[overflow-wrap:anywhere]` to be retained on the " +
        "description block so long unbroken strings wrap mid-token " +
        "(the dialog body's vertical scroll doesn't help horizontal " +
        "overflow).",
    ).toBe(true)
  })
})
