import { describe, expect, it } from "vitest"

import { deriveMount } from "./urls"

// ADR-0020: the dashboard derives its mount prefix from the URL at
// runtime so ONE build serves both the tailnet (/agent-mcp/…) and a
// Traefik root front door (/…). The critical tailnet-regression guard is
// that a /agent-mcp/… path still derives exactly "/agent-mcp".
describe("deriveMount", () => {
  it("returns /agent-mcp for tailnet app paths (byte-identical)", () => {
    expect(deriveMount("/agent-mcp/app/foo/")).toBe("/agent-mcp")
    expect(deriveMount("/agent-mcp/app/")).toBe("/agent-mcp")
    expect(deriveMount("/agent-mcp/app/foo/?page=tasks")).toBe("/agent-mcp")
    expect(deriveMount("/agent-mcp/api/foo/all-data")).toBe("/agent-mcp")
    expect(deriveMount("/agent-mcp/assets/chunk.js")).toBe("/agent-mcp")
    expect(deriveMount("/agent-mcp/login")).toBe("/agent-mcp")
  })

  it("returns '' for the root front door (Traefik at host root)", () => {
    expect(deriveMount("/app/foo/")).toBe("")
    expect(deriveMount("/app/")).toBe("")
    expect(deriveMount("/api/foo/all-data")).toBe("")
    expect(deriveMount("/login")).toBe("")
  })

  it("supports an arbitrary proxy-chosen prefix", () => {
    expect(deriveMount("/tools/agent-mcp/app/foo/")).toBe("/tools/agent-mcp")
  })

  it("falls back to /agent-mcp with no window (SSR/prerender)", () => {
    // In vitest's node env there is no window; the no-arg call uses the
    // SSR default so build-time prerender is unaffected.
    expect(deriveMount()).toBe("/agent-mcp")
  })
})
