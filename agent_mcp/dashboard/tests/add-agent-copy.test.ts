/**
 * Pin the user-facing copy for the agent-creation flow.
 *
 * Originally surfaced by Dennis's critical review on 2026-06-17 (against
 * v5.0.48): the Agents tab had a "Deploy" button + "Deploy Agent" modal
 * title that did NOT actually deploy anything — the underlying tool only
 * registered a row + token. The fix renamed the labels to "Add".
 *
 * Wave 7 PR 3 (coordinator transition, 2026-06-29) deleted the legacy
 * `CreateAgentModal` and the spawn-via-tmux `create_agent_tool_impl`
 * that backed it. The sole agent-creation surface is now
 * `RegisterAgentModal`, whose trigger + dialog title both read
 * "Register Agent" — distinct from the original "Add" copy because the
 * register-only flow now hands back a snippet the operator pastes into
 * the user's claude config (the operator's action genuinely is "register
 * this agent on the backend", not just "add a row"). The "no-Deploy"
 * copy guard from the original review still applies.
 */

import { describe, expect, it } from "vitest"
import { agentsPageSource } from "./support/agents-source"

// The Agents page is a page module + a directory of satellites since the
// <DataTablePage> migration (RegisterAgentModal now lives in
// components/dashboard/agents/register-agent-modal.tsx); the copy guards
// below read all of it (see tests/support/agents-source.ts).

// ---------- RegisterAgentModal: trigger + submit copy -----------------

describe("RegisterAgentModal trigger + submit copy", () => {
  it("trigger button says 'Register Agent'", () => {
    // The Agents-tab header button that opens the agent-creation dialog
    // must read `Register Agent`.
    const src = agentsPageSource()
    expect(
      src.includes("Register Agent"),
      "RegisterAgentModal trigger button must render the label " +
        "'Register Agent'.",
    ).toBe(true)
    // Bound the negative check to the dialog regions (the file
    // otherwise contains correct uses of 'deployment' in code comments
    // about path-prefix deployments).
    expect(
      src.includes("Deploy Agent"),
      "Dashboard has resurrected the misleading 'Deploy Agent' modal " +
        "copy — rename to 'Register Agent'.",
    ).toBe(false)
  })

  it("does not use Deploy submit copy", () => {
    // RegisterAgentModal's submit copy must not regress to the
    // misleading `Deploy` / `Deploying...` strings.
    const src = agentsPageSource()
    expect(
      !src.includes("'Deploying...'") && !src.includes("'Deploy'"),
      "Dashboard still uses the 'Deploy' / 'Deploying...' submit copy " +
        "somewhere — the agent-creation flow does not deploy anything; " +
        "rename to 'Register' / 'Registering...'.",
    ).toBe(true)
  })
})

// ---------- Empty-state copy ------------------------------------------

describe("empty-state copy", () => {
  it("says 'Add your first agent'", () => {
    // The empty-state shown when no agents exist must invite the user
    // to `Add your first agent` — not `Deploy your first agent`.
    const src = agentsPageSource()
    expect(
      src.includes("Add your first agent to get started."),
      "Empty-state copy must say 'Add your first agent to get " +
        "started.' (was 'Deploy your first agent ...')",
    ).toBe(true)
    expect(
      src.includes("Deploy your first agent to get started."),
      "Empty-state still says 'Deploy your first agent ...' — rename " +
        "to 'Add'.",
    ).toBe(false)
  })
})
