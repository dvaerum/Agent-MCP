/**
 * Regression-guard pins for the mobile sidebar toggle visibility bug.
 *
 * Background. On narrow viewports the sidebar (rendered as a Sheet
 * overlay via the shadcn `<Sidebar>` primitive) ends up covering the
 * entire viewport, including the `<Header>` row that hosts the hamburger
 * toggle. Combined with two other defects this leaves users with **no
 * visible way to dismiss the sidebar**:
 *
 *   1. `SheetContent`'s built-in close (X) button is hidden via the
 *      `[&>button]:hidden` selector on the shadcn `<Sidebar>` mobile
 *      branch (components/ui/sidebar.tsx line 190). That's an upstream
 *      shadcn default the dashboard inherits.
 *
 *   2. AppSidebar's effect at lines 38-42 force-opens the mobile sheet
 *      whenever `isMobile && state === 'expanded'`. If the user closes
 *      the sheet but the `state` is still expanded (it's a separate
 *      desktop state machine), the next re-render flicks it back open.
 *
 *   3. The header `<Menu>` button is rendered behind the sheet — its
 *      z-index (50) does not exceed the SheetContent overlay's z-index.
 *
 * This file pins the structural fix:
 *
 *   - The mobile sheet variant must render a visible close affordance
 *     that's reachable while the sheet is open (a Button rendered
 *     inside SidebarHeader for `isMobile`).
 *   - The header hamburger toggle must remain visible at every viewport
 *     below `lg:` (no `md:hidden`/`sm:hidden` on the trigger).
 *   - The trigger button must carry an accessible label
 *     ("Toggle navigation menu" or "Toggle sidebar") via `sr-only`
 *     span / `aria-label`.
 *   - The force-open mobile effect must NOT re-fire while the user has
 *     explicitly dismissed the sheet.
 *
 * Tests parse .tsx source — no dashboard runtime needed (pure-Node
 * source-grep, same convention as the rest of this suite).
 *
 * Ported from tests/test_dashboard_sidebar_toggle_mobile.py (Python
 * source tree retired).
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const HEADER = resolve(DASHBOARD_ROOT, "components/layout/header.tsx")
const APP_SIDEBAR = resolve(DASHBOARD_ROOT, "components/layout/app-sidebar.tsx")

function read(p: string): string {
  return readFileSync(p, "utf8")
}

// ---------------------------------------------------------------------------
// Header hamburger toggle — must be visible at every viewport < lg
// ---------------------------------------------------------------------------

describe("header hamburger toggle visibility", () => {
  it("remains visible below the lg breakpoint (no md:hidden/sm:hidden/xl:hidden)", () => {
    // Regression we are pinning: prior versions of the dashboard hid the
    // button at narrower breakpoints (`md:hidden` or `sm:hidden`),
    // leaving a viewport range where the only entry-point to the
    // sidebar disappeared. It is the primary open-sidebar affordance on
    // tablet (768-1023 px) where the in-sidebar Panel toggle is also
    // present, and it MUST exist on mobile (<768 px) so the user can
    // re-open the sheet after dismissing it.
    const src = read(HEADER)

    // The Menu icon button must exist.
    expect(src.includes("Menu"), "header.tsx must import + render lucide Menu icon").toBe(
      true,
    )
    expect(
      src.includes("toggleSidebar"),
      "header.tsx must wire toggleSidebar from useSidebar",
    ).toBe(true)

    // Locate the Menu button's className. It should use lg:hidden
    // (visible 0-1023, hidden 1024+) and MUST NOT use md:hidden /
    // sm:hidden which would create a dead range.
    const menuBlockMatch =
      /<Button[^>]*onClick=\{toggleSidebar\}[^>]*?className=\{?"([^"]+)"/s.exec(
        src,
      )
    expect(
      menuBlockMatch,
      "Could not locate the header Menu toggle <Button> in header.tsx; " +
        'test parsing assumes onClick={toggleSidebar} + className="..."',
    ).not.toBeNull()

    const classes = menuBlockMatch![1]!

    const forbidden = ["md:hidden", "sm:hidden", "xl:hidden"]
    for (const cls of forbidden) {
      expect(
        classes.includes(cls),
        `Header hamburger toggle uses '${cls}' which would hide it at ` +
          `viewports < lg; this re-introduces the bug (full classes: ${JSON.stringify(classes)}).`,
      ).toBe(false)
    }

    // Positive: lg:hidden is the correct gate.
    expect(
      classes.includes("lg:hidden"),
      "Header hamburger toggle must use lg:hidden so it's visible at " +
        `every viewport < 1024 px (current classes: ${JSON.stringify(classes)}).`,
    ).toBe(true)
  })

  it("carries an accessible label (sr-only span or aria-label)", () => {
    // Look for an sr-only span near the Menu button. We match the
    // canonical shadcn pattern: <span className="sr-only">…</span>
    // somewhere inside the Menu <Button>.
    const src = read(HEADER)
    const pattern = /<Button[^>]*onClick=\{toggleSidebar\}.*?<\/Button>/s
    const btnMatch = pattern.exec(src)
    expect(btnMatch, "Could not locate header Menu Button block").not.toBeNull()

    const btnBody = btnMatch![0]
    const hasSrOnly =
      btnBody.includes('className="sr-only"') || btnBody.includes("className='sr-only'")
    const hasAria = btnBody.includes("aria-label=")
    expect(
      hasSrOnly || hasAria,
      "Header hamburger toggle must have a sr-only label span or aria-label " +
        `for screen-reader accessibility. Button block: ${JSON.stringify(btnBody)}`,
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Mobile sheet — must have a visible close button INSIDE the sheet
// ---------------------------------------------------------------------------

describe("in-sheet mobile close button", () => {
  it("renders a visible close affordance wired to setOpenMobile(false)", () => {
    // When the sidebar is rendered as a Sheet overlay on mobile
    // (isMobile === true), there must be a visible close affordance
    // INSIDE the sheet's own DOM. The header's hamburger is behind the
    // sheet's z-index when the sheet is open, so without an in-sheet
    // close button the user is trapped.
    //
    // Pin: app-sidebar.tsx must render a Button (with X / PanelLeftClose
    // icon) gated on `isMobile` inside SidebarHeader, wired to
    // setOpenMobile(false).
    const src = read(APP_SIDEBAR)

    expect(
      /setOpenMobile\(\s*false\s*\)/.test(src),
      "app-sidebar.tsx must render an in-sheet close button that calls " +
        "setOpenMobile(false). The header toggle is behind the sheet on " +
        "mobile, so an in-sheet close is the only way out.",
    ).toBe(true)

    // And it should be wired to an X / PanelLeftClose / Menu icon.
    const hasCloseIcon = ["X,", "X ", "X}", "PanelLeftClose"].some((name) =>
      src.includes(name),
    )
    expect(
      hasCloseIcon,
      "app-sidebar.tsx mobile-close button must use a recognisable close " +
        "icon (X from lucide-react or PanelLeftClose).",
    ).toBe(true)
  })

  it("carries an accessible label (Close sidebar/menu/navigation)", () => {
    // Look for "Close sidebar" or "Close menu" or "Toggle sidebar" in a
    // sr-only span near setOpenMobile(false).
    const src = read(APP_SIDEBAR)
    const closePhrases = ["Close sidebar", "Close menu", "Close navigation"]
    const hasLabel = closePhrases.some((phrase) => src.includes(phrase))
    expect(
      hasLabel,
      "Mobile close button must announce itself to screen readers. " +
        `Expected one of ${JSON.stringify(closePhrases)} in a sr-only span or aria-label.`,
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Force-open effect must respect explicit user dismissal
// ---------------------------------------------------------------------------

describe("force-open mobile effect does not loop", () => {
  it("no setOpenMobile(true) effect depends on `state`", () => {
    // The mobile auto-open effect must not re-fire on every render
    // while the user has explicitly dismissed the sheet. The previous
    // implementation watched `[isMobile, state, setOpenMobile]` and
    // called `setOpenMobile(true)` whenever `state === 'expanded'`,
    // which meant closing the sheet (which only changes `openMobile`,
    // not `state`) left the effect armed to re-open on the next tick.
    //
    // The fix removes the effect entirely (the SidebarProvider's
    // `defaultOpen` + mobile sheet's own open/close state already give
    // the user the right initial experience) OR guards it so it only
    // runs once on the isMobile transition.
    const src = read(APP_SIDEBAR)

    // The simplest robust pin: the auto-open effect, if it still
    // exists, must NOT depend on `state` (which doesn't change when
    // the user closes the sheet). We check that no `useEffect` block
    // both calls setOpenMobile(true) AND has `state` in its
    // dependency array.
    const effectPattern =
      /React\.useEffect\(\s*\(\)\s*=>\s*\{([\s\S]*?)\}\s*,\s*\[([\s\S]*?)\]\s*\)/g
    let m: RegExpExecArray | null
    while ((m = effectPattern.exec(src)) !== null) {
      const [, body, deps] = m
      const callsOpenTrue = /setOpenMobile\(\s*true\s*\)/.test(body!)
      if (callsOpenTrue) {
        expect(
          deps!.includes("state"),
          "An auto-open effect that calls setOpenMobile(true) must not " +
            "depend on `state` — `state` doesn't change when the user " +
            "closes the sheet, so this would re-fire and re-open the " +
            `sheet on every render. Effect deps: [${deps}]`,
        ).toBe(false)
      }
    }
  })
})

// ---------------------------------------------------------------------------
// Cross-check: trigger button is reachable at every viewport
// ---------------------------------------------------------------------------

describe("every viewport has a sidebar dismiss affordance", () => {
  it("combines header + in-sheet + in-sidebar toggles into one guarantee", () => {
    // Combine the above into a single end-to-end assertion: at every
    // viewport break, the user has at least one visible toggle.
    //
    //   0-767 px (mobile, sheet overlay):
    //     - Header Menu button (lg:hidden = visible)
    //     - In-sheet close button (isMobile gated)
    //   768-1023 px (tablet, desktop sidebar):
    //     - Header Menu button (lg:hidden = visible)
    //     - In-sidebar PanelLeftClose (!isMobile gated)
    //   1024+ px (desktop):
    //     - In-sidebar PanelLeftClose (!isMobile gated)
    const headerSrc = read(HEADER)
    const sidebarSrc = read(APP_SIDEBAR)

    // 0-1023: header trigger present and visible
    expect(headerSrc.includes("toggleSidebar") && headerSrc.includes("lg:hidden")).toBe(
      true,
    )

    // 0-767: in-sheet close
    expect(
      /setOpenMobile\(\s*false\s*\)/.test(sidebarSrc),
      "Missing in-sheet mobile close button",
    ).toBe(true)

    // 768+: in-sidebar PanelLeft toggle
    expect(
      sidebarSrc.includes("PanelLeftClose") || sidebarSrc.includes("PanelLeftOpen"),
      "Missing in-sidebar desktop toggle",
    ).toBe(true)
  })
})
