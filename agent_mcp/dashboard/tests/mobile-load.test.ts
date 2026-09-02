/**
 * Regression guards for the mobile-load PR (verify-all + mobile diag,
 * 2026-06-28). Independent fixes shipped in one PR; each gets a
 * source-shape assertion here so a future refactor can't silently
 * undo any of them. (The graph-loader double-fetch guard that used to
 * live here was removed along with the System page and its
 * collaboration-graph library.)
 *
 * Why grep-based: the dashboard test harness is intentionally
 * node-only (see vitest.config.ts) — no jsdom, no React-testing-
 * library. The properties we owe — "every section dashboard is loaded
 * via next/dynamic", "Overview no longer mounts the collaboration
 * graph" — are properties of the SOURCE, not the runtime, so source
 * assertions are the right shape and they sit alongside the existing
 * mcp-notifications-no-poll guard.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

describe("mobile-load regression guards", () => {
  describe("A — page.tsx: per-section code-split via next/dynamic", () => {
    const src = read("app/page.tsx")

    it("uses next/dynamic to load every section dashboard", () => {
      expect(
        /import\s+dynamic\s+from\s+["']next\/dynamic["']/.test(src),
        "app/page.tsx must import `dynamic` from 'next/dynamic'.",
      ).toBe(true)

      // Each section dashboard component must be created via
      // `dynamic(() => import(...))`. The dashboards listed here are
      // the eight sections rendered by the section-router switch
      // statement in DashboardPage().
      const expected = [
        "overview-dashboard",
        "projects-overview-dashboard",
        "agents-dashboard",
        "tasks-dashboard",
        "memories-dashboard",
        "messages-dashboard",
        "settings-dashboard",
        "prompt-book-dashboard",
      ]
      for (const mod of expected) {
        const re = new RegExp(
          String.raw`dynamic\(\s*\(\s*\)\s*=>\s*import\(["'][^"']*${mod}["']\)`,
        )
        expect(
          re.test(src),
          `app/page.tsx must load \`${mod}\` via next/dynamic.`,
        ).toBe(true)
      }
    })

    it("disables SSR for every dynamic section import", () => {
      // ssr: false matters for two reasons:
      //   1. Every section uses "use client" + browser-only globals,
      //      so SSR would just throw during prerender.
      //   2. Without ssr: false the static-export pass drags the
      //      section trees back into the route bundle, defeating the
      //      split.
      const matches = src.match(/dynamic\(/g) ?? []
      const ssrFalseMatches = src.match(/ssr:\s*false/g) ?? []
      expect(
        ssrFalseMatches.length >= matches.length,
        `Every dynamic() call in app/page.tsx must pass \`ssr: false\` ` +
          `(saw ${matches.length} dynamic() calls but only ${ssrFalseMatches.length} \`ssr: false\` markers).`,
      ).toBe(true)
    })

    it("does not statically import any section dashboard at module top-level", () => {
      // A static `import { FooDashboard } from "@/components/dashboard/foo-dashboard"`
      // would silently un-do the split. Allow imports from
      // `@/components/dashboard/dashboard-wrapper` (the shell) — only
      // the section dashboards must stay dynamic.
      const offending = [
        "overview-dashboard",
        "projects-overview-dashboard",
        "agents-dashboard",
        "tasks-dashboard",
        "memories-dashboard",
        "messages-dashboard",
        "settings-dashboard",
        "prompt-book-dashboard",
      ]
      for (const mod of offending) {
        const re = new RegExp(
          String.raw`^\s*import\s+\{[^}]*\}\s+from\s+["'][^"']*\/${mod}["']`,
          "m",
        )
        expect(
          re.test(src),
          `app/page.tsx must not statically import \`${mod}\` — use next/dynamic.`,
        ).toBe(false)
      }
    })
  })

  describe("E — Overview no longer mounts VisGraph", () => {
    const src = read("components/dashboard/overview-dashboard.tsx")

    it("does not import or render <VisGraph>", () => {
      expect(
        /from\s+["'][^"']*vis-graph[^"']*["']/.test(src),
        "overview-dashboard.tsx must not import any vis-graph module — the " +
          "graph was removed entirely.",
      ).toBe(false)
      expect(
        /<\s*VisGraph[\s>]/.test(src),
        "overview-dashboard.tsx must not render <VisGraph>.",
      ).toBe(false)
    })

    it("does not import or render <NodeDetailPanel>", () => {
      // Without the graph there is nothing to click into, so the node
      // detail panel must be gone too.
      expect(
        /from\s+["'][^"']*node-detail-panel[^"']*["']/.test(src),
        "overview-dashboard.tsx must not import node-detail-panel.",
      ).toBe(false)
      expect(
        /<\s*NodeDetailPanel[\s>]/.test(src),
        "overview-dashboard.tsx must not render <NodeDetailPanel>.",
      ).toBe(false)
    })
  })
})
