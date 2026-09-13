/**
 * Path-prefix deployments must show a connected dashboard without the
 * `proxy:0 Disconnected` ghost entry.
 *
 * Regression context. PR #23 added `apiClient.setBaseUrl` so dashboard
 * fetches under path-prefixed deployments (`/agent-mcp/__dashboard/<name>/`)
 * go through the router proxy. PR's `ApiClientInitializer` then auto-seeds
 * a `server-store` entry so the upstream sidebar gate is satisfied. The
 * seed uses placeholder `host: 'proxy', port: 0` because the entry needs
 * *something* for the existing `setServer(host, port)` flow.
 *
 * That breaks two ways in production:
 *
 * 1. **Connection bug.** `serverStore.setActiveServer` calls
 *    `apiClient.setServer(host, port)`, which overwrites baseUrl to
 *    `http://proxy:0/api`. The earlier `setBaseUrl('/agent-mcp/__api/<name>')`
 *    from the seed is lost. `checkServerHealth` then fails (no `proxy:0`
 *    host) and the entry status flips to `'error'`. The cold-start retry
 *    loop re-triggers setActiveServer → same overwrite → never connects.
 *    Dennis sees "Disconnected" forever.
 *
 * 2. **Display bug.** The sidebar (`server-connection.tsx`, the
 *    management modal, overview-dashboard) renders `{server.host}:{server.port}`
 *    verbatim — so the user sees the literal string `proxy:0` under the
 *    project name. Pure cosmetic, but visible and confusing.
 *
 * Fix shape. Add an optional `baseUrl?: string` field to `MCPServer`.
 * When set, `setActiveServer` and `checkServerHealth` call
 * `apiClient.setBaseUrl(server.baseUrl)` instead of
 * `apiClient.setServer(host, port)`. Display components hide host:port
 * when `baseUrl` is present. The auto-seed in `ApiClientInitializer`
 * passes `baseUrl: '/agent-mcp/__api/<name>'` when seeding.
 *
 * These are regression guards — they parse the .ts/.tsx as text rather
 * than execute it. The fix is verified end-to-end via `npm run build`
 * plus a Firefox MCP click-through documented on the PR.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

const STORE = "lib/stores/server-store.ts"
// Candidate C refactor moved the auto-seed side effect out of the old
// `api-client-initializer.tsx` and into the module-load body of
// `lib/project-context.ts`. The seed still happens — just synchronously
// at module import (gated on `persist.onFinishHydration` as before)
// instead of via a React effect.
const INIT = "lib/project-context.ts"
const CONN = "components/server/server-connection.tsx"
const MODAL = "components/server/server-management-modal.tsx"
const OVERVIEW = "components/dashboard/overview-dashboard.tsx"

describe("path-prefix explicit baseUrl (server-store)", () => {
  it("`MCPServer` interface declares an optional baseUrl?: string field", () => {
    const src = read(STORE)
    expect(
      src.includes("baseUrl?: string"),
      "expected `baseUrl?: string` on the MCPServer interface in " +
        "server-store.ts — path-prefix entries need an explicit field " +
        "rather than the `host: 'proxy', port: 0` sentinel that " +
        "currently leaks into the UI as 'proxy:0 Disconnected'.",
    ).toBe(true)
  })

  it("`setActiveServer` calls apiClient.setBaseUrl when the entry carries an explicit baseUrl", () => {
    const src = read(STORE)
    const ok =
      src.includes("setBaseUrl(server.baseUrl") ||
      src.includes("setBaseUrl(activeServer.baseUrl") ||
      src.includes("setBaseUrl(s.baseUrl")
    expect(
      ok,
      "expected `setActiveServer` in server-store.ts to call " +
        "`apiClient.setBaseUrl(server.baseUrl)` when the entry has one. " +
        "Otherwise the path-prefix override from " +
        "ApiClientInitializer is clobbered and the dashboard fetches " +
        "from `http://proxy:0/api`.",
    ).toBe(true)
  })

  it("`checkServerHealth` likewise honors an explicit baseUrl instead of unconditional host:port", () => {
    const src = read(STORE)
    // Allow either pattern: branch on baseUrl, or a single
    // `setBaseUrl(server.baseUrl ?? \`http://...\`)` ternary.
    const setBaseUrlCount = src.split("setBaseUrl").length - 1
    const healthyBranch =
      src.includes("server.baseUrl") && setBaseUrlCount >= 2 // at least one in setActiveServer + one in checkServerHealth
    expect(
      healthyBranch,
      "expected `checkServerHealth` in server-store.ts to honor " +
        "`server.baseUrl` (call `apiClient.setBaseUrl(...)`) rather " +
        "than calling `setServer(host, port)` unconditionally.",
    ).toBe(true)
  })

  it("the PathPrefix singleton's auto-seed passes baseUrl to addServer via the centralised apiUrl() helper", () => {
    const src = read(INIT)
    // PR-B centralised the API URL build in lib/urls.ts; the singleton
    // now goes through `apiUrl()` instead of templating the URL inline.
    // The literal lives in lib/urls.ts, the import lives here.
    expect(
      src.includes("apiUrl"),
      "expected lib/project-context.ts to import the apiUrl() helper " +
        "from lib/urls.ts (PR-B centralisation)",
    ).toBe(true)
    const urlsSrc = read("lib/urls.ts")
    expect(
      urlsSrc.includes("/agent-mcp/api"),
      "expected the path-prefix API root literal /agent-mcp/api in " +
        "lib/urls.ts — needed so the persisted server entry knows " +
        "where to fetch via the router proxy.",
    ).toBe(true)
    expect(
      src.includes("baseUrl") && src.includes("addServer"),
      "expected the auto-seed `addServer(...)` call to include a " +
        "baseUrl field; otherwise the persisted entry only carries " +
        "the placeholder host:port and the connection loop fails.",
    ).toBe(true)
  })

  it("sidebar / modal / overview consult server.baseUrl to hide host:port for path-prefix entries", () => {
    for (const path of [CONN, MODAL, OVERVIEW]) {
      const src = read(path)
      // Either guard the host:port render with `!server.baseUrl`, or
      // show server.baseUrl instead, or drop the render entirely when
      // baseUrl exists. Accept any signal that the file is aware of
      // the field.
      expect(
        src.includes("baseUrl"),
        `expected ${path} to consult \`server.baseUrl\` and hide ` +
          `\`host:port\` when set; otherwise path-prefix entries ` +
          `render as 'proxy:0' in the UI.`,
      ).toBe(true)
    }
  })
})
