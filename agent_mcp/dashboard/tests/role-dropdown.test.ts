/**
 * Dashboard test for the Role dropdown on Add Agent / Edit Agent.
 *
 * Phase 2 Wave 2b (prancy-napping-pie §2e) wired the dashboard's role
 * dropdown so operators can choose `worker` (default) or `manager`
 * when creating or editing an agent.
 *
 * Wave 7 PR 3 (coordinator transition, 2026-06-29) deleted the legacy
 * `CreateAgentModal` along with the spawn-via-tmux
 * `create_agent_tool_impl`. The sole agent-creation surface is now
 * `RegisterAgentModal` (Wave 7 PR 0), which carries its own role
 * dropdown. Edit-agent flow keeps its role dropdown. The
 * `apiClient.createAgent` typing pin retires with the legacy method;
 * `editAgent` still carries `agent_role?:`.
 *
 * The repo has no jsdom; behaviour is verified via `npm run build`
 * plus Firefox-MCP e2e against the VM. The tests here are source-grep
 * guards (matches the sibling *-polish.test.ts files in this suite).
 *
 * Ported from tests/test_dashboard_role_dropdown.py (Python source
 * tree retired).
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { agentsPageSource } from "./support/agents-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

// The Agents page is a page module + a directory of satellites since the
// <DataTablePage> migration (the Register / Edit dialogs each own a file
// now); the role-dropdown guards read all of it (see
// tests/support/agents-source.ts).

const API_TS = "lib/api/agents.ts"

// ---------- RegisterAgentModal: Role dropdown + form state -------------

describe("RegisterAgentModal role dropdown", () => {
  it("form state defaults role to 'worker'", () => {
    const src = agentsPageSource()
    expect(
      src.includes("role: 'worker'") || src.includes('role: "worker"'),
      "RegisterAgentModal form state must default role to 'worker'",
    ).toBe(true)
  })

  it("renders a Select with manager + worker options bound to the form's role field", () => {
    const src = agentsPageSource()
    expect(
      src.includes('value="worker"') || src.includes("value='worker'"),
      "Role dropdown must offer 'worker' as a SelectItem value",
    ).toBe(true)
    expect(
      src.includes('value="manager"') || src.includes("value='manager'"),
      "Role dropdown must offer 'manager' as a SelectItem value",
    ).toBe(true)
  })

  it("submit handler forwards role to the registerAgent payload", () => {
    const src = agentsPageSource()
    // `registerAgent({ name: ..., role: formData.role, ... })` is the
    // literal shape the modal builds. Pin on the substring.
    expect(
      src.includes("role: formData.role"),
      "RegisterAgentModal must pass role: formData.role to " +
        "apiClient.registerAgent",
    ).toBe(true)
  })
})

// ---------- EditAgentDialog: Role dropdown + diff logic ----------------

describe("EditAgentDialog role dropdown", () => {
  it("holds agentRole state seeded from the current row's agent_role", () => {
    const src = agentsPageSource()
    expect(
      (src.includes("useState") && src.includes("agentRole")) ||
        src.includes("setAgentRole"),
      "EditAgentDialog must declare agentRole state for the Role dropdown",
    ).toBe(true)
  })

  it("diffs agent_role into the updates payload", () => {
    const src = agentsPageSource()
    // The Edit Agent save handler must include `agent_role` in the
    // updates payload when the dropdown's value differs from the
    // agent's current role — matches the diff pattern used for
    // capabilities / color / working_directory / aoe_session_id /
    // auto_event_loop.
    expect(
      src.includes("updates.agent_role"),
      "EditAgentDialog must assign updates.agent_role when the role " +
        "field has changed",
    ).toBe(true)
  })
})

// ---------- api.ts client typings carry agent_role --------------------

describe("apiClient typings", () => {
  it("editAgent's updates arg type accepts agent_role", () => {
    // Wave 7 PR 3 deleted apiClient.createAgent (legacy spawn path);
    // the equivalent typing pin for createAgent retired with it.
    const src = read(API_TS)
    expect(
      src.includes("agent_role?:"),
      "apiClient.editAgent updates type must declare optional " +
        "agent_role?: (worker | manager)",
    ).toBe(true)
  })
})
