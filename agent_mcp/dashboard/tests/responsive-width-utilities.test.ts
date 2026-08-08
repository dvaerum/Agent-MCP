/**
 * The `!important` width override that killed every responsive width
 * utility (fix/agents-status-badge-overflow).
 *
 * Source-text guards in the house style (pure Node — see
 * tests/ux-polish.test.ts for the rationale).
 *
 * `app/globals.css` carried, inside `@layer components`:
 *
 *     .w-full { width: 100% !important; }
 *
 * commented "Ensure full width layouts don't have unwanted margins".
 * It is redundant with Tailwind's own `.w-full` (which lives in the
 * later `@layer utilities` and therefore already wins) — except for the
 * `!important`, which made it beat EVERY responsive override. Because
 * an `!important` declaration inside a cascade layer outranks both
 * unlayered rules and later layers, `w-full sm:w-40` resolved to 100%
 * at all breakpoints. Verified in Firefox on the live dashboard: the
 * Messages filter bar's six controls each computed to 924.8px and
 * stacked one per row, pushing the table to y=894 on a 800px-tall
 * viewport. Deleting exactly this one rule from the live stylesheet
 * (CSSOM `deleteRule`) put all six controls on one row and moved the
 * table to y=546, with the phone layout (390px) unchanged.
 *
 * The idiom it broke is used by six dashboards, so the guard is
 * written against the whole stylesheet, not just `.w-full`.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const globals = readFileSync(resolve(DASHBOARD_ROOT, "app/globals.css"), "utf8")

/** Strip comments so a commented-out example can't trip the sweep. */
const css = globals.replace(/\/\*[\s\S]*?\*\//g, "")

describe("globals.css must not !important-override Tailwind utilities", () => {
  it("does not redeclare .w-full", () => {
    expect(
      /\.w-full\s*\{/.test(css),
      "globals.css must not redeclare .w-full — Tailwind's utilities " +
        "layer already provides it, and a components-layer copy can " +
        "only ever shadow a responsive override.",
    ).toBe(false)
  })

  it("declares no !important width/height on a bare utility selector", () => {
    // A bare `.<utility> { … !important }` in globals.css always beats
    // the `sm:` / `md:` / `lg:` form of the same utility.
    const offenders = Array.from(
      css.matchAll(/(^|\})\s*(\.[a-z0-9-]+)\s*\{([^}]*!important[^}]*)\}/gi),
    )
      .filter(([, , , body]) => /\b(width|height|min-width|max-width)\s*:/i.test(body))
      .map(([, , sel]) => sel)
    expect(offenders, `!important sizing on ${offenders.join(", ")}`).toEqual([])
  })
})

describe("the w-full + responsive-width idiom is actually load-bearing", () => {
  it("is used by the migrated dashboards", () => {
    // If this ever goes to zero the guards above are dead weight and
    // can go with it; while it holds, they protect real pages.
    const src = readFileSync(
      resolve(DASHBOARD_ROOT, "components/dashboard/messages-dashboard.tsx"),
      "utf8",
    )
    expect(/w-full sm:w-\d/.test(src)).toBe(true)
  })
})
