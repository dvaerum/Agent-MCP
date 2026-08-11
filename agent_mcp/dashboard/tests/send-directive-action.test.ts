/**
 * Send-directive (ad-hoc poke) reachability regression guard.
 *
 * Bug
 * ---
 * The poke — push a one-shot directive to an agent, delivered
 * immediately if it's parked in wait_for_events, else queued as its
 * highest-priority next check-in — was wired ONLY as a per-row button on
 * the Schedules page (`onClick={() => setPokeAgentId(s.agent_id)}` on a
 * scheduled_directive row). An agent with no schedule had no row, so
 * there was no way to poke it from the UI at all — the exact scenario an
 * operator hit.
 *
 * Fix
 * ---
 * The poke UX lives in one shared `SendDirectiveModal`, mounted on:
 *   - the Agents page (a per-agent "Send directive" row/detail action),
 *     so ANY agent is reachable regardless of whether it has a schedule;
 *   - the Schedules page (a standalone top-of-page control with an agent
 *     picker, NOT tied to a schedule row).
 *
 * These are source-text assertions (matching the grep-based convention
 * of tests/mcp-notifications-no-poll.test.ts) — the property we owe is
 * "the button exists on the Agents page and both pages route through the
 * shared modal", which is a property of the wiring, not the runtime.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) => readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

describe("send-directive reachable from the Agents page (no schedule required)", () => {
  const agents = read("components/dashboard/agents-dashboard.tsx")
  // Post-<DataTablePage> migration the desktop row markup lives in the
  // Agents column spec, and the mobile card in its own module. Both are
  // satellites of the same page, so the wiring assertions below name the
  // file that actually owns each half.
  const columns = read("components/dashboard/agents/agent-columns.tsx")
  const mobileCard = read("components/dashboard/agents-mobile-list.tsx")

  it("Agents page imports and renders the shared SendDirectiveModal", () => {
    expect(agents).toContain(
      'import { SendDirectiveModal } from "@/components/dashboard/shared/send-directive-modal"',
    )
    expect(agents).toContain("<SendDirectiveModal")
    expect(agents).toContain("lockedAgentId={directiveAgentId}")
  })

  it("Agents page wires a per-agent send-directive row action", () => {
    // The action handler is threaded down to the column spec (desktop
    // row), the mobile card, and the detail dialog.
    expect(agents).toContain("const handleSendDirective = useCallback((agentId: string)")
    expect(agents).toContain("onSendDirective: handleSendDirective,")
    expect(agents).toContain("onSendDirective={handleSendDirective}")
    // Both row surfaces render a data-testid-tagged trigger for the poke.
    expect(columns).toContain("send-directive-${agent.agent_id}")
    expect(mobileCard).toContain("send-directive-mobile-${agent.agent_id}")
  })
})

describe("send-directive standalone on the Schedules page (not tied to a row)", () => {
  const schedules = read("components/dashboard/schedules-dashboard.tsx")

  it("Schedules page routes the poke through the shared modal, not an inline one", () => {
    expect(schedules).toContain(
      'import { SendDirectiveModal } from "@/components/dashboard/shared/send-directive-modal"',
    )
    expect(schedules).toContain("<SendDirectiveModal")
    // The old inline poke state/handler must be gone.
    expect(schedules).not.toContain("submitPoke")
    expect(schedules).not.toContain("setPokeAgentId")
  })

  it("Schedules page exposes a standalone send-directive button (agent picker)", () => {
    expect(schedules).toContain('data-testid="send-directive-btn"')
    // Standalone control opens the modal with no locked target → picker.
    expect(schedules).toContain("openDirective(null)")
  })
})

describe("shared SendDirectiveModal distinguishes delivered vs queued", () => {
  const modal = read("components/dashboard/shared/send-directive-modal.tsx")

  it("reads `delivered` from the poke response and branches the toast copy", () => {
    expect(modal).toContain("res.delivered")
    expect(modal).toContain("Delivered to")
    expect(modal).toContain("Queued for")
  })

  it("surfaces the agent's live listening state before sending", () => {
    expect(modal).toContain("wait_for_events_in_flight")
  })

  it("supports both a locked target and a picker (dual mode)", () => {
    expect(modal).toContain("lockedAgentId")
    expect(modal).toContain("<AgentSelect")
  })

  it("force-refetches all-data on open so the picker isn't empty on the Schedules page", () => {
    // Wave 6: the picker + listening hint read the shared `/all-data`
    // TanStack Query. On the standalone Schedules page the query may be
    // idle, so the modal calls `refresh()` (the awaitable force-refetch
    // from useAllDataStatus) on open to hydrate the picker + the live
    // `wait_for_events_in_flight` flag. Regression guard for the
    // cold-load empty-picker bug.
    expect(modal).toContain("useAllDataStatus")
    expect(modal).toContain("void refresh()")
  })
})
