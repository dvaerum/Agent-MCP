/**
 * Regression guards for the Agents page row icon refresh.
 *
 * The Agents table previously surfaced a 3-dot (`MoreVertical`) kebab
 * menu on every row, but the kebab had no click handlers — it was dead
 * UI.
 *
 * This PR replaces the kebab with explicit per-row icon buttons and
 * swaps the existing eye-icon's sidebar drawer for a Dialog-based
 * detail modal:
 *
 * - Active rows: Edit (Pencil), Delete (Trash2), View (Eye)
 * - Terminated rows: Restore + Purge (kept exactly as PR #38 shipped)
 * - View opens a Dialog modal (not a Sheet / sidebar)
 * - Edit opens a Dialog modal wired to a new editAgent API client method
 *
 * Text-parse regression guards (house source-grep convention, matching
 * the agent-restore-purge test).
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

// ---------- Kebab removed -----------------------------------------

describe("kebab menu removed", () => {
  it("no MoreVertical / MoreHorizontal kebab", () => {
    // The 3-dot kebab (MoreVertical) had no handlers and was confusing.
    // It must be gone from the agents-dashboard.tsx row markup.
    const src = read("components/dashboard/agents-dashboard.tsx")
    expect(
      src.includes("MoreVertical"),
      "agents-dashboard.tsx must not import or render MoreVertical " +
        "(the 3-dot kebab) — every row action should be an explicit icon",
    ).toBe(false)
    // Belt-and-suspenders: also the lucide alias.
    expect(
      src.includes("MoreHorizontal"),
      "agents-dashboard.tsx must not render MoreHorizontal either",
    ).toBe(false)
  })
})

// ---------- Edit icon ---------------------------------------------

describe("edit icon", () => {
  it("has an edit icon button", () => {
    // Edit button uses the lucide Pencil (or Edit) icon and wires a
    // click handler that opens the edit modal.
    const src = read("components/dashboard/agents-dashboard.tsx")
    // Either Pencil or Edit icon must be imported.
    expect(
      src.includes("Pencil") ||
        (src.includes("\nimport") &&
          src.includes("Edit") &&
          !src.includes("Edit2")),
      "agents-dashboard.tsx must import a Pencil/Edit icon for the " +
        "edit-agent row button",
    ).toBe(true)
    // The handler indirection — opening the edit dialog from a row click.
    expect(
      src.includes("onEdit") ||
        src.includes("handleEdit") ||
        src.includes("setEditAgent"),
      "agents-dashboard.tsx must wire an edit click handler " +
        "(onEdit / handleEdit / setEditAgent) on the row",
    ).toBe(true)
  })

  it("renders an EditAgentDialog", () => {
    // An EditAgentDialog (Dialog-based) must render — title contains
    // 'Edit agent' so the admin sees what they're doing.
    const src = read("components/dashboard/agents-dashboard.tsx")
    expect(
      src.includes("EditAgentDialog") ||
        src.includes("Edit agent") ||
        src.includes("Edit Agent"),
      "agents-dashboard.tsx must include an Edit Agent dialog " +
        "(component name EditAgentDialog or title text 'Edit agent')",
    ).toBe(true)
  })
})

// ---------- Delete icon -------------------------------------------

describe("delete icon", () => {
  it("has a delete icon with confirmation", () => {
    // Delete (Trash2) icon on active rows triggers terminate via a
    // confirmation dialog — not a bare onClick that immediately POSTs.
    const src = read("components/dashboard/agents-dashboard.tsx")
    // Trash2 is already used by Purge; it must also be used to label
    // the active-row Delete button OR a dedicated TerminateAgentDialog
    // must wrap the confirmation. We accept either signal.
    expect(
      src.includes("Trash2"),
      "Trash2 icon must be present (used by Purge already, and reused " +
        "for the active-row Delete icon)",
    ).toBe(true)
    // The confirmation dialog title text — admin sees this before the
    // destructive action fires.
    expect(
      src.includes("Terminate agent"),
      "agents-dashboard.tsx must include a Terminate confirmation " +
        "dialog with title text 'Terminate agent ...?'",
    ).toBe(true)
  })
})

// ---------- View / details modal ----------------------------------

describe("view / details modal", () => {
  it("uses Dialog, not Sheet", () => {
    // The eye (view) icon used to open `AgentDetailsPanel` — a fixed
    // sidebar drawer. It must now open a Dialog modal instead.
    const src = read("components/dashboard/agents-dashboard.tsx")
    expect(
      src.includes("AgentDetailsPanel"),
      "agents-dashboard.tsx must no longer mount AgentDetailsPanel " +
        "(the sidebar drawer); the view-icon now opens a Dialog modal",
    ).toBe(false)
    // The new modal component / inline Dialog markup.
    expect(
      src.includes("AgentDetailDialog") ||
        src.includes("Agent details") ||
        src.includes("Agent Details"),
      "agents-dashboard.tsx must include a dialog-based view modal " +
        "(component name AgentDetailDialog or title 'Agent details')",
    ).toBe(true)
  })

  it("renders all agent fields", () => {
    // The view modal must surface all the agent's fields.
    const src = read("components/dashboard/agents-dashboard.tsx")
    // User-facing labels expected in the dialog body.
    // Working Directory, Color, Token preview, Created.
    for (const label of [
      "Agent ID",
      "Status",
      "Created",
      "Working Directory",
      "Color",
      "Token",
    ]) {
      expect(
        src.includes(label),
        `view dialog must include the label ${JSON.stringify(label)}`,
      ).toBe(true)
    }
  })
})

// ---------- API client edit endpoint ------------------------------

describe("API client edit endpoint", () => {
  it("has editAgent", () => {
    // A new `editAgent` method must exist on the API client, hitting
    // the upstream POST /api/agents/<id>/edit route added by this PR.
    const src = read("lib/api/agents.ts")
    expect(
      src.includes("editAgent"),
      "api.ts must export editAgent for the dashboard Edit button",
    ).toBe(true)
    expect(
      src.includes("/edit"),
      "editAgent must POST to the /edit endpoint",
    ).toBe(true)
  })
})
