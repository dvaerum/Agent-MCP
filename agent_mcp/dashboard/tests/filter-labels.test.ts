/**
 * Regression-guard pins for the dashboard filter-bar labeling sweep.
 *
 * Background. `messages-dashboard.tsx` was the one page that had already
 * solved a real problem: pick a value in a filter dropdown and the field
 * it belongs to disappears — "Assigned" or "High" sitting there with no
 * indication of which filter it came from. Messages fixed this with a
 * private `FilterField` helper (a small visible label stacked above each
 * control) alongside an `aria-label`/`ariaLabel` on the control itself —
 * but the fix was never promoted to `shared/`, so it stayed invisible to
 * every other list page.
 *
 * The audit that followed found the SAME bug, independently regressed, on
 * four more pages:
 *
 *   * Tasks — Status / Assignment / Created-by / Priority selects had
 *     neither a visible label nor an `aria-label` at all. Once picked,
 *     a value carries zero field context.
 *   * Agents — Status select, same gap.
 *   * Memories — the sort-order select, same gap (and it isn't even a
 *     filter — it's a sort, with nothing distinguishing it from one).
 *   * Prompt Book — Category select, same gap.
 *   * Schedules — had `aria-label`s already (the accessible half was
 *     fine) but no visible label (the sighted-user half wasn't).
 *
 * `shared/filter-field.tsx` now holds the promoted `<FilterField>`. This
 * file pins that every filter/sort control in a list page's filter bar
 * carries BOTH halves of the fix — a nearby visible label (sighted users)
 * and an `aria-label`/`ariaLabel` (screen readers) — so a future filter
 * control can't ship half-labeled again.
 *
 * Scope is deliberately the top-level list-page dashboards only (the
 * same `*-dashboard.tsx` set the CC-7 mobile-polish audit uses) — NOT
 * every `<Select>` in every create/edit dialog across the app. A create-
 * dialog's own form fields are a different, already-adjacent concern
 * (most already use a real `<Label htmlFor>`, the standard shadcn form
 * idiom) and dialog-hosted tables/selects were deliberately scoped out of
 * CC-7 for the same reason: a different bar for a different UI role.
 *
 * Some top-level dashboard files ALSO embed such a dialog inline (e.g.
 * prompt-book-dashboard.tsx's Prompt Builder, whose per-variable fields
 * pair `<Label htmlFor={variable.name}>` with `id={variable.name}` on the
 * control) — a real `for`/`id` association is the native HTML idiom and
 * strictly satisfies both halves of this audit on its own, so it's
 * recognized as a pass rather than special-cased out of the file glob.
 * Tests parse the .tsx source; no dashboard runtime needed.
 */

import { describe, expect, it } from "vitest"
import { readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const DASHBOARDS = resolve(DASHBOARD_ROOT, "components", "dashboard")

function read(path: string): string {
  return readFileSync(path, "utf8")
}

// Comments legitimately mention `<SelectTrigger`/`<FilterField` when
// explaining this very audit (see this file's own docstring, and any
// in-source comment doing the same) — strip comments before scanning so
// a doc-comment can't count as a real control or a real label.
const JSX_COMMENT_RE = /\{\/\*[\s\S]*?\*\/\}/g
const BLOCK_COMMENT_RE = /\/\*[\s\S]*?\*\//g
const LINE_COMMENT_RE = /^\s*\/\/.*$/gm

function codeOnly(src: string): string {
  src = src.replace(JSX_COMMENT_RE, "")
  src = src.replace(BLOCK_COMMENT_RE, "")
  return src.replace(LINE_COMMENT_RE, "")
}

const TAG_START_RE = /<(SelectTrigger|AgentSelect)\b/g

/**
 * The JSX opening tag starting at `start` (the '<'), scanning to its
 * OWN closing '>' while treating `{...}` JSX-expression braces as
 * opaque. A prop like `onChange={(v) => f(v)}` contains a literal '>'
 * from the arrow function that is NOT the tag's end — a naive
 * `[^>]*>` regex stops there and silently truncates the tag, which is
 * exactly the trap `<AgentSelect onChange={(v) => ...}>` sets.
 */
function tagSpan(src: string, start: number): string {
  let depth = 0
  let i = start
  while (i < src.length) {
    const ch = src[i]
    if (ch === "{") {
      depth += 1
    } else if (ch === "}") {
      depth -= 1
    } else if (ch === ">" && depth === 0) {
      return src.slice(start, i + 1)
    }
    i += 1
  }
  return src.slice(start)
}

// A visible label nearby: either the promoted `<FilterField>` wrapper or
// a real shadcn `<Label>` (the form-field idiom used elsewhere — an
// equally-valid, pre-existing way to satisfy "visible label").
const VISIBLE_LABEL_RE = /<(FilterField|Label)\b/

// How far back from a control's own tag to look for its visible label.
// Generous enough to span a `<div className="relative">` wrapper (the
// search-input idiom) or a multi-line prop list, tight enough that it
// won't wander into an unrelated PRIOR control's label.
const LABEL_LOOKBACK = 400

// A control's `id=` attribute — either a string literal or a JSX
// expression — and a `<Label htmlFor=...>` using the same value is the
// native HTML label-association idiom (e.g. prompt-book-dashboard's
// per-variable builder fields: `<Label htmlFor={variable.name}>` paired
// with `<SelectTrigger id={variable.name}>`). It's STRICTLY better than
// either `aria-label` or a merely-nearby `<FilterField>`/`<Label>` — a
// real `for`/`id` pairing is what actually wires the two together for
// assistive tech — so a control with this pairing satisfies BOTH the
// accessible-name and visible-label requirements outright, regardless
// of distance (an unambiguous exact match, unlike an unlinked nearby
// label that could just be lucky proximity).
const ID_ATTR_RE = /\bid=(\{[^}]*\}|"[^"]*")/
const LABEL_FOR_RE = /<Label\b[^>]*?\bhtmlFor=(\{[^}]*\}|"[^"]*")/gs

function hasMatchingLabelFor(src: string, tag: string): boolean {
  const idMatch = ID_ATTR_RE.exec(tag)
  if (!idMatch) {
    return false
  }
  const controlId = idMatch[1]
  for (const m of src.matchAll(LABEL_FOR_RE)) {
    if (m[1] === controlId) {
      return true
    }
  }
  return false
}

/**
 * The list-page dashboards in scope for this audit — same top-level
 * `*-dashboard.tsx` glob the CC-7 audit uses, restricted to the ones
 * that actually render a `<SelectTrigger>`/`<AgentSelect>` at all (a
 * page with no dropdown filter has nothing for this audit to check).
 */
function filterBarDashboards(): string[] {
  const found: string[] = []
  const entries = readdirSync(DASHBOARDS).filter(
    (f) => f.endsWith("-dashboard.tsx"),
  )
  entries.sort()
  for (const f of entries) {
    const path = resolve(DASHBOARDS, f)
    const src = read(path)
    if (src.includes("<SelectTrigger") || src.includes("<AgentSelect")) {
      found.push(path)
    }
  }
  return found
}

describe("filter/sort controls are self-describing", () => {
  it("every <SelectTrigger>/<AgentSelect> in a list page's filter bar has both a visible label and an accessible name", () => {
    // Every `<SelectTrigger>` / `<AgentSelect>` in a list page's filter
    // bar must have BOTH a nearby visible label (`<FilterField>` or
    // `<Label>`) and an `aria-label`/`ariaLabel` on the control itself.
    // Missing either one reproduces the exact bug found on
    // Tasks/Agents/Memories/Prompt Book: a value that gives no
    // indication of which filter it belongs to, for a sighted user (no
    // visible label) or a screen reader user (no accessible name)
    // respectively.
    const files = filterBarDashboards()
    expect(files.length, "filter-control audit set derived empty — the derivation is broken").toBeGreaterThan(0)

    const failures: string[] = []
    for (const f of files) {
      const src = codeOnly(read(f))
      for (const m of src.matchAll(TAG_START_RE)) {
        const kind = m[1]!
        const tag = tagSpan(src, m.index!)
        const line = src.slice(0, m.index!).split("\n").length
        if (hasMatchingLabelFor(src, tag)) {
          continue
        }
        const ariaAttr = kind === "SelectTrigger" ? "aria-label=" : "ariaLabel="
        const hasAria = tag.includes(ariaAttr)
        const lookbackStart = Math.max(0, m.index! - LABEL_LOOKBACK)
        const hasVisibleLabel = VISIBLE_LABEL_RE.test(
          src.slice(lookbackStart, m.index!),
        )
        if (!hasAria) {
          failures.push(
            `${f}:${line}  <${kind}> missing ${ariaAttr} ` +
              "(no accessible name for screen readers)",
          )
        }
        if (!hasVisibleLabel) {
          failures.push(
            `${f}:${line}  <${kind}> has no nearby <FilterField> or ` +
              "<Label> (no visible name for sighted users — a " +
              "picked value gives no indication of which filter " +
              "it belongs to)",
          )
        }
      }
    }
    expect(
      failures,
      "Filter/sort control(s) missing a visible label and/or " +
        "aria-label (filter-bar labeling sweep):\n  " + failures.join("\n  "),
    ).toEqual([])
  })
})
