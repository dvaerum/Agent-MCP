/**
 * Regression guards for the dashboard path-prefix derivation.
 *
 * Originally these guarded the 3-useEffect bootstrap in
 * `api-client-initializer.tsx` (auto-seed + cold-start retry +
 * hydration-gate). Candidate C from the 2026-06-01 architecture review
 * collapsed that into:
 *
 *   - A module-level singleton in `lib/project-context.ts` that derives
 *     `{projectName, baseUrl, apiPrefix}` synchronously from
 *     `window.location.pathname` at import time (no useEffect, no
 *     zustand-persist hydration race).
 *   - Transparent retry inside `ApiClient.request()` on 502/503/504
 *     (no boundary-level setInterval poll).
 *
 * These guards check the new module. The "cold-start retry" guard moved
 * to the path-prefix-adapter guard (asserting the retry loop in
 * `lib/api.ts`), and the "hydration-gate" guard is gone — the new
 * derivation is synchronous and cannot race the persisted state.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

const PROJECT_CONTEXT = "lib/project-context.ts"

describe("dashboard path-prefix derivation (project-context singleton)", () => {
  it("derives the path prefix using the shared dashboard-path regex", () => {
    // The PathPrefix singleton must inspect window.location.pathname
    // for the deployment URL pattern so the dashboard self-bootstraps
    // when mounted under /agent-mcp/app/<name>/ (PR-B renamed from
    // /__dashboard/). The regex literal moved to lib/urls.ts (PR-B
    // centralisation); project-context.ts imports the matcher.
    const src = read(PROJECT_CONTEXT)
    expect(
      src.includes("APP_PROJECT_PATH_RE"),
      "expected project-context.ts to import APP_PROJECT_PATH_RE " +
        "from lib/urls.ts (PR-B centralisation)",
    ).toBe(true)
    const urlsSrc = read("lib/urls.ts")
    expect(
      urlsSrc.includes("/agent-mcp/app"),
      "expected the path-prefix regex `/agent-mcp/app` in lib/urls.ts; " +
        "derivation only works when the deployment URL pattern is detected",
    ).toBe(true)
    expect(
      src.includes("window.location.pathname"),
      "expected the singleton to read window.location.pathname " +
        "(synchronous derivation at module import)",
    ).toBe(true)
  })

  it("guards the derivation with a `typeof window` SSR check", () => {
    // Next.js prerenders the module at build time where `window` is
    // undefined. The singleton must guard with `typeof window` to fall
    // through to defaults during SSR.
    const src = read(PROJECT_CONTEXT)
    expect(
      src.includes("typeof window"),
      "expected `typeof window !== 'undefined'` SSR guard so the " +
        "module imports cleanly during Next.js prerender",
    ).toBe(true)
  })
})
