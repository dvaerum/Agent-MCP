/**
 * Regression guards for the dashboard Restore + Purge UI.
 *
 * The Agents page table previously only had `[Terminate]` for active
 * agents and nothing for terminated agents. This PR adds:
 *
 * - A `restoreAgent` and `purgeAgent` (with `getPurgePreview`) on
 *   the api.ts client.
 * - Buttons in agents-dashboard.tsx that fire on terminated rows.
 * - A confirmation modal rendering blast-radius counts before purge.
 * - Removal of the dashboard filter that hides terminated agents.
 *
 * These tests parse the .tsx/.ts files as text (no jsdom/RTL — matching
 * the convention in the house source-grep test style). They catch
 * regression if someone removes the wiring.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { agentsPageSource } from "./support/agents-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const readDashboard = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// The Agents page is a page module + a directory of satellites since the
// <DataTablePage> migration; guards about "the Agents page" read all of
// it (see tests/support/agents-source.ts).
function read(rel: string): string {
  if (rel === "components/dashboard/agents-dashboard.tsx") {
    return agentsPageSource()
  }
  return readDashboard(rel)
}

describe("api client restore/purge methods", () => {
  it("has restoreAgent", () => {
    const src = read("lib/api/agents.ts")
    expect(
      src.includes("restoreAgent"),
      "api.ts must export restoreAgent for the dashboard Restore button",
    ).toBe(true)
    expect(
      src.includes("/restore"),
      "restoreAgent must hit the /restore endpoint",
    ).toBe(true)
  })

  it("has purgeAgent and getPurgePreview", () => {
    const src = read("lib/api/agents.ts")
    expect(src.includes("purgeAgent"), "api.ts must export purgeAgent").toBe(
      true,
    )
    expect(
      src.includes("getPurgePreview"),
      "api.ts must export getPurgePreview for the confirmation modal",
    ).toBe(true)
    expect(
      src.includes("cascade"),
      "purgeAgent must include cascade=true on the DELETE request",
    ).toBe(true)
    expect(
      src.includes("purge-preview"),
      "getPurgePreview must hit /purge-preview",
    ).toBe(true)
  })
})

describe("agents-dashboard renders restore and purge", () => {
  it("surfaces Restore and Purge buttons on terminated rows", () => {
    const src = read("components/dashboard/agents-dashboard.tsx")
    expect(
      src.includes("Restore"),
      "agents-dashboard.tsx must surface a 'Restore' button on terminated rows",
    ).toBe(true)
    expect(
      src.includes("Purge"),
      "agents-dashboard.tsx must surface a 'Purge' button on terminated rows",
    ).toBe(true)
    // The buttons must wire to apiClient methods (handler indirection OK).
    expect(
      src.includes("restoreAgent") || src.includes("handleRestore"),
    ).toBe(true)
    expect(
      src.includes("purgeAgent") ||
        src.includes("handlePurge") ||
        src.includes("PurgeAgentDialog"),
    ).toBe(true)
  })

  it("lists terminated agents", () => {
    // The Agents table previously hid terminated agents (used
    // `getActiveAgents()` which filters them out). To show
    // Restore/Purge, the page must include terminated rows in the
    // rendered list.
    const src = read("components/dashboard/agents-dashboard.tsx")
    // Heuristic: either no longer uses getActiveAgents-only filter, OR
    // has an explicit reference to terminated rows being included.
    // Accept any one of these signals.
    const signals = [
      "showTerminated",
      "includeTerminated",
      "data?.agents", // iterates raw agents list
      "data.agents",
      "agent.status === 'terminated'",
      "all agents",
    ]
    expect(
      signals.some((s) => src.includes(s)),
      "agents-dashboard.tsx must somehow include terminated agents in " +
        `the rendered list; looked for any of ${JSON.stringify(signals)}`,
    ).toBe(true)
  })

  it("renders a purge confirmation dialog component", () => {
    // A dedicated purge confirmation dialog component (or inline modal)
    // must render the preview counts so admins see blast-radius before
    // confirming.
    const src = read("components/dashboard/agents-dashboard.tsx")
    const hasDialog =
      src.includes("Confirm purge") || src.includes("Purge agent")
    expect(
      hasDialog,
      "agents-dashboard.tsx must contain a purge confirmation modal " +
        "with copy like 'Confirm purge' or 'Purge agent'",
    ).toBe(true)
    // And must reference the preview shape (counts.*).
    expect(
      src.includes("counts") || src.includes("messages_sent"),
      "purge confirmation must surface the preview counts to the admin",
    ).toBe(true)
  })
})
