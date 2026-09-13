/**
 * Regression guards for the dashboard project picker.
 *
 * The picker rewrite swaps "switch server-store entries" for "fetch
 * project list from a router endpoint and navigate via
 * window.location.href" — the deployment is multi-tenant by URL path,
 * not by per-server connections.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const PICKER = resolve(
  DASHBOARD_ROOT,
  "components",
  "server",
  "project-picker.tsx",
)

const read = () => readFileSync(PICKER, "utf8")

describe("dashboard project picker", () => {
  it("fetches the router projects endpoint via useProjectsStore", () => {
    // As of Phase 3.5b the picker reads the project list from the
    // cross-project useProjectsStore (backed by /agent-mcp/__overview)
    // instead of fetching /agent-mcp/__projects directly. The store
    // indirection lets the picker consume the same envelope the
    // overview cards do — one network round-trip per tab, and the
    // tenancy mode (multi vs single) is available in the same payload.
    const src = read()
    expect(
      src.includes("useProjectsStore"),
      "expected picker to consume useProjectsStore (the cross-" +
        "project store backed by /agent-mcp/__overview) instead of " +
        "fetching /__projects directly",
    ).toBe(true)
  })

  it("navigates via window.location.href", () => {
    const src = read()
    expect(
      src.includes("window.location.href"),
      "expected picker to navigate via window.location.href = " +
        "appUrl(<name>) rather than switching server-store entries " +
        "(PR-B routes through lib/urls.ts helpers)",
    ).toBe(true)
    // PR-B centralised URLs in lib/urls.ts; picker now imports
    // `appUrl()` instead of templating the URL inline.
    expect(
      src.includes("appUrl"),
      "expected the picker to import appUrl() from lib/urls.ts " +
        "(PR-B centralisation)",
    ).toBe(true)
  })

  it("drops the Add Server dialog", () => {
    // Multi-tenant deployment has no concept of manually adding a
    // server connection — projects come from the router.
    const src = read()
    // The "Add Server" dialog used Dialog from @/components/ui/dialog
    expect(
      !src.includes("Dialog") || src.includes("DropdownMenu"),
      "expected the Add-Server Dialog to be removed (or at least " +
        "DropdownMenu used as primary UI)",
    ).toBe(true)
  })
})
