/**
 * Regression guards for URL-driven dashboard section routing.
 *
 * Background. The dashboard's "active section" (Overview / Agents /
 * Tasks / Memories / Messages / Settings / Prompt Book) used to live
 * exclusively in zustand state (`useDashboard.currentView`). Reloading
 * the page reset the view to Overview, and there was no way to share a
 * URL that pointed at a non-Overview section — both broke Dennis's
 * expected behaviour ("click Tasks, reload, end up on Tasks"; "paste
 * URL while on Agents, recipient sees Agents").
 *
 * The fix wires the active section to the URL via the `?page=<section>`
 * query parameter. Reasons for the query-param shape (vs proper
 * Next.js route segments):
 *
 *   * No Next.js app-router file restructuring is required (the
 *     dashboard is a single client page that switches a `currentView`
 *     enum) — query-param keeps the diff small.
 *   * Coexists cleanly with the path-prefix adapter from PR #56 that
 *     mounts the dashboard at `/agent-mcp/__dashboard/<project>/`. The
 *     shape becomes `/agent-mcp/__dashboard/<project>/?page=tasks`.
 *   * `useSearchParams` is the canonical Next.js hook for this; no
 *     `dynamicParams` / `generateStaticParams` plumbing needed.
 *   * Bookmarks + share-links work out of the box.
 *   * Missing param falls back to Overview, matching legacy behaviour.
 *
 * The tests parse `.tsx` source — no jsdom/RTL infrastructure in this
 * fork (same lightweight pattern as
 * test_dashboard_sidebar_toggle_mobile.py, test_dashboard_path_prefix_adapter.py,
 * now ported to this vitest convention).
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const APP_PAGE = resolve(DASHBOARD_ROOT, "app", "page.tsx")
const NAVIGATION = resolve(
  DASHBOARD_ROOT,
  "components",
  "layout",
  "navigation.tsx",
)
const USE_SECTION_ROUTE = resolve(
  DASHBOARD_ROOT,
  "lib",
  "use-section-route.ts",
)

function read(p: string): string {
  return readFileSync(p, "utf8")
}

// ---------------------------------------------------------------------------
// Section enum — must be a single source of truth
// ---------------------------------------------------------------------------

const EXPECTED_SECTIONS = new Set([
  "overview",
  "agents",
  "tasks",
  "memories",
  "messages",
  "schedules",
  "settings",
  "prompts",
])

describe("dashboard URL section routing", () => {
  it("has section enum values that are URL-safe (lowercase alphanumeric, no spaces / special chars) — all current sections happen to be single words, pinning this so a future rename to e.g. 'Prompt Book' as the URL value would fail the test instead of producing `?page=Prompt%20Book` in share-links", () => {
    for (const s of EXPECTED_SECTIONS) {
      expect(
        /^[a-z][a-z0-9-]*$/.test(s),
        `Section enum value ${JSON.stringify(s)} is not URL-safe; expected lowercase alphanumeric/-`,
      ).toBe(true)
    }
  })

  // ---------------------------------------------------------------------------
  // The use-section-route hook — single source of truth for URL <-> section
  // ---------------------------------------------------------------------------

  it("has a dedicated use-section-route hook — a small dedicated hook centralises the URL <-> section bridging so that page.tsx and the Navigation component both read/write through the same code path; extracting it to lib/use-section-route.ts keeps page.tsx readable and makes the URL contract testable from one location", () => {
    let exists = true
    try {
      read(USE_SECTION_ROUTE)
    } catch {
      exists = false
    }
    expect(
      exists,
      `Expected dedicated hook at ${USE_SECTION_ROUTE} that wraps useSearchParams + useRouter to expose \`currentSection\` + \`setSection(section)\`.`,
    ).toBe(true)
  })

  it("reads the active section from URL search params — this is the load-bearing piece that makes reload-stable navigation work", () => {
    const src = read(USE_SECTION_ROUTE)
    expect(
      src.includes("useSearchParams"),
      "use-section-route.ts must import and call `useSearchParams` from next/navigation to read the `?page=` query.",
    ).toBe(true)
    // The query key must be `page` (the chosen URL shape).
    expect(
      /['"]page['"]/.test(src),
      "use-section-route.ts must reference the 'page' search-param key — that is the agreed URL shape.",
    ).toBe(true)
  })

  it("writes the section via the Next.js router (router.push / router.replace) so subsequent reloads + browser back/forward work correctly", () => {
    const src = read(USE_SECTION_ROUTE)
    expect(
      src.includes("useRouter"),
      "use-section-route.ts must import `useRouter` from next/navigation.",
    ).toBe(true)
    // Either replace or push is acceptable. replace avoids polluting
    // the back stack on every nav-click; push enables back/forward
    // through history. We allow either but require one.
    expect(
      /router\.(replace|push)\s*\(/.test(src),
      "use-section-route.ts setter must call router.push / router.replace to write the new ?page=… into the URL.",
    ).toBe(true)
  })

  it("defaults missing `?page` to 'overview' so the bare dashboard URL keeps working unchanged", () => {
    const src = read(USE_SECTION_ROUTE)
    // Looking for an "overview" default — accept either the literal
    // 'overview' string used as a fallback, or a constant named with
    // 'overview' in it.
    expect(
      src.includes("'overview'") || src.includes('"overview"'),
      "use-section-route.ts must reference 'overview' as the default section when ?page= is missing or invalid.",
    ).toBe(true)
  })

  it("falls back to the default (overview) for unknown / typo'd `?page=` values rather than breaking rendering", () => {
    const src = read(USE_SECTION_ROUTE)
    // The hook should validate the param against the known enum. The
    // simplest way to pin this is to require at least the section
    // union or a guard helper. We check that the source references
    // most section names (validation against the known set).
    let referenced = 0
    for (const s of EXPECTED_SECTIONS) {
      if (src.includes(`'${s}'`) || src.includes(`"${s}"`)) referenced++
    }
    expect(
      referenced >= 4,
      "use-section-route.ts must reference the section enum values for " +
        "input validation — guard against unknown ?page= values by " +
        "falling back to 'overview'. Found " +
        `${referenced} of ${EXPECTED_SECTIONS.size} sections referenced.`,
    ).toBe(true)
  })

  // ---------------------------------------------------------------------------
  // page.tsx — must read active section from the URL, not from zustand alone
  // ---------------------------------------------------------------------------

  it("has app/page.tsx derive the rendered section from the URL via the use-section-route hook (or useSearchParams directly) — reading only from zustand state means reload always lands on Overview, which is the bug we are fixing", () => {
    const src = read(APP_PAGE)
    const usesHook = src.includes("useSectionRoute")
    const usesSearchParams = src.includes("useSearchParams")
    expect(
      usesHook || usesSearchParams,
      "app/page.tsx must read the active section via useSectionRoute " +
        "(from lib/use-section-route.ts) or useSearchParams (from " +
        "next/navigation) so the URL drives the rendered section. " +
        "Reading from useDashboard.currentView only means reload " +
        "loses the section.",
    ).toBe(true)
  })

  it("has the switch in page.tsx continue to handle every known section so the URL → component map stays in sync with the enum", () => {
    const src = read(APP_PAGE)
    for (const section of EXPECTED_SECTIONS) {
      // Each case appears as case 'overview': etc.
      expect(
        src.includes(`case '${section}'`) || src.includes(`case "${section}"`),
        `page.tsx must have a \`case\` for section ${JSON.stringify(section)}`,
      ).toBe(true)
    }
  })

  // ---------------------------------------------------------------------------
  // Navigation — sidebar items must update the URL on click
  // ---------------------------------------------------------------------------

  it("has clicking a sidebar nav item update the URL so reload + share-links work — implementation must call either useSectionRoute's setter or router.push/replace with the new ?page= value", () => {
    const src = read(NAVIGATION)
    const usesHook = src.includes("useSectionRoute")
    const usesRouterPush = /router\.(replace|push)\s*\(/.test(src)
    expect(
      usesHook || usesRouterPush,
      "navigation.tsx must update the URL on nav-click — either by " +
        "calling the useSectionRoute setter or by calling " +
        "router.push/replace with the new ?page= param.",
    ).toBe(true)
  })

  it("does not have the nav onClick be a bare `setCurrentView(item.view)` call without also updating the URL — pin the regression: if the onClick only updates zustand, reload won't preserve the section", () => {
    const src = read(NAVIGATION)
    // The previous (buggy) onClick was effectively:
    //     onClick={() => { setCurrentView(item.view); if (isMobile) setOpenMobile(false); }}
    // Detect that exact pattern with no URL write nearby. We check the
    // whole file: if `useSectionRoute` is imported OR `router.push` is
    // called, we're fine. Otherwise, the file is the old buggy shape.
    const hasUrlWrite =
      src.includes("useSectionRoute") || /router\.(replace|push)\s*\(/.test(src)
    expect(
      hasUrlWrite,
      "navigation.tsx onClick must write to the URL (via useSectionRoute " +
        "setter or router.push/replace), not only call zustand " +
        "setCurrentView. Otherwise reload loses the section.",
    ).toBe(true)
  })

  it("has the NavItem.view union in navigation.tsx match the agreed URL-section enum so /?page=<view> always maps to a real menu item", () => {
    const src = read(NAVIGATION)
    // Find the `view: '...'` union in the NavItem interface.
    const match = src.match(/view:\s*((?:'[a-z]+'\s*\|\s*)+'[a-z]+')/)
    expect(
      match,
      "navigation.tsx must declare the NavItem.view union literally " +
        "(view: 'overview' | 'agents' | ...). Could not locate.",
    ).not.toBeNull()
    const declared = new Set(
      [...match![1]!.matchAll(/'([a-z]+)'/g)].map((m) => m[1]!),
    )
    expect(
      declared,
      `navigation.tsx view union ${JSON.stringify([...declared])} does not match the expected URL-section enum ${JSON.stringify([...EXPECTED_SECTIONS])}.`,
    ).toEqual(EXPECTED_SECTIONS)
  })
})
