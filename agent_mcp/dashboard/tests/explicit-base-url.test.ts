/**
 * Regression guards: dashboard ApiClient + ApiClientInitializer support
 * explicit base URLs for path-prefixed deployments.
 *
 * Today's flow: ApiClientInitializer auto-seeds a synthetic server
 * entry with `(host='proxy', port=0)` when the dashboard URL matches
 * `/agent-mcp/__dashboard/<name>/`, then calls setServer(proxy, 0).
 * The fork's PR #7 setServer produces `http://proxy:0/api` — broken.
 *
 * Two changes here let deployments avoid the broken URL without an
 * out-of-tree patch:
 *
 * 1. New ApiClient.setBaseUrl(url: string) accepts an explicit base
 *    URL. Used as an alternative to setServer(host, port) when the
 *    caller already knows the API root.
 *
 * 2. ApiClientInitializer, when the path-prefix matches, calls
 *    apiClient.setBaseUrl with the derived URL
 *    (`/agent-mcp/__api/<name>`) so subsequent fetches resolve through
 *    the router's proxy instead of the broken `http://proxy:0/api`.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

describe("explicit base URL support (ApiClient + path-prefix singleton)", () => {
  it("ApiClient exposes a setBaseUrl(url) method", () => {
    const src = read("lib/api/client.ts")
    expect(
      src.includes("setBaseUrl"),
      "expected ApiClient to expose a setBaseUrl(url: string) method " +
        "so deployments can override baseUrl without going through " +
        "setServer's host:port construction",
    ).toBe(true)
  })

  it("the path-prefix singleton calls apiClient.setBaseUrl with the centralised apiUrl() helper", () => {
    // Candidate C refactor (2026-06-01) moved this side effect from
    // the old api-client-initializer.tsx useEffect into the
    // module-load body of `lib/project-context.ts`. When the
    // path-prefix matches, the singleton must call
    // `apiClient.setBaseUrl` with the derived `/agent-mcp/api/<name>`
    // URL (PR-B renamed from /__api/) so the very first fetch already
    // routes through the proxy.
    const src = read("lib/project-context.ts")
    expect(
      src.includes("setBaseUrl"),
      "expected lib/project-context.ts to call apiClient.setBaseUrl " +
        "with the derived URL when the dashboard URL matches " +
        "/agent-mcp/app/<name>/",
    ).toBe(true)
    // PR-B centralised the URL build in lib/urls.ts; project-context
    // now imports `apiUrl()` instead of templating the URL inline.
    expect(
      src.includes("apiUrl"),
      "expected lib/project-context.ts to import the apiUrl() helper " +
        "from lib/urls.ts (PR-B centralisation)",
    ).toBe(true)
    const urlsSrc = read("lib/urls.ts")
    expect(
      urlsSrc.includes("/agent-mcp/api"),
      "expected the path-derived URL prefix /agent-mcp/api in " +
        "lib/urls.ts",
    ).toBe(true)
  })
})
