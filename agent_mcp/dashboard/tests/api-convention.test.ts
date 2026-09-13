/**
 * Regression guards for the dashboard API client URL convention.
 *
 * The convention this PR establishes: `ApiClient.baseUrl` IS the API
 * root (includes the `/api` path segment when present). All URL
 * construction inside ApiClient just appends the endpoint to baseUrl —
 * no method hardcodes `/api`. Hand-built fetches outside ApiClient go
 * through `apiClient.request` rather than concatenating onto
 * `getServerUrl()`.
 *
 * These tests parse the .ts(x) files as text rather than executing
 * JavaScript. They catch regression (someone reintroduces a hardcoded
 * `/api` segment) but don't test runtime behavior. Runtime is verified
 * by `npm run build` (which must compile) plus manual verification in
 * the PR body.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) => readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

describe("dashboard API client URL convention", () => {
  it("setServer appends /api to baseUrl", () => {
    const src = read("lib/api/client.ts")
    // find the setServer method body (until next blank-line/method)
    const m = src.match(/setServer\([^)]*\)\s*\{([\s\S]*?)\n\s*\}/)
    expect(m, "setServer not found in api.ts").not.toBeNull()
    const body = m![1]!
    expect(
      body.includes("/api"),
      "setServer must set baseUrl with `/api` suffix so request() " +
        "doesn't have to hardcode it; got body:\n" + body,
    ).toBe(true)
  })

  it("does not hardcode the /api segment after baseUrl", () => {
    // api.ts must not have `${baseUrl}/api` patterns — baseUrl IS the
    // API root.
    //
    // Catches `${this.baseUrl}/api${endpoint}` in request(),
    // `${this.baseUrl}/api${endpoint}` in createEventSource(),
    // `${this.baseUrl}/api/health` in testCORS(), etc.
    const src = read("lib/api/client.ts")
    // Match either `${baseUrl}/api...` or `${this.baseUrl}/api...`
    const bad = [...src.matchAll(/\$\{(?:this\.)?baseUrl\}\/api/g)]
    expect(
      bad.length === 0,
      `found ${bad.length} occurrences of \`\${baseUrl}/api\` in api.ts; ` +
        "baseUrl should already include /api, so the literal `/api` is " +
        "redundant and causes double-prefix bugs in path-routed " +
        `deployments. Matches: ${JSON.stringify(bad.map((m) => m[0]))}`,
    ).toBe(true)
  })

  it("data-store does not hand-build API URLs", () => {
    // data-store.ts must route through apiClient.request, not concat
    // /api/.
    const src = read("lib/stores/data-store.ts")
    // Forbid the specific pattern `apiClient.getServerUrl()...api`
    expect(
      src.includes("getServerUrl()}/api"),
      "data-store.ts must not hand-build URLs with " +
        "`getServerUrl()/api/...`; use apiClient.request('/...') instead.",
    ).toBe(false)
  })
})
