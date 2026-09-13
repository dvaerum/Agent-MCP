/**
 * The Agents page row count labelled "N assigned" must mean
 * "open / still-to-do tasks assigned to this agent", NOT "total tasks
 * that were ever assigned to this agent" — the latter is what the
 * current code computes, which is why ios-app-dev shows 18 "assigned"
 * in washing-brothers when 16 of them are completed.
 *
 * This is a UI-only filter: the dashboard already has the task rows
 * locally (it gets them from /api/all-data). The fix is to AND the
 * existing `assigned_to` filter with `status NOT IN
 * ('completed', 'cancelled', 'failed')` in TWO places in the Agents
 * page's task-stats derivation:
 *
 *   1. `taskStats.assigned` — drives the row's text "{n} assigned"
 *      pill on the Agents table.
 *   2. The same filter pattern duplicated inside the row body.
 *
 * This regression guard greps the source file(s) for the predicate
 * (text-parse pattern matches the other dashboard tests in this repo)
 * because the project ships no jsdom/RTL setup for full behavioral
 * coverage of this derivation.
 *
 * The row markup moved out of agents-dashboard.tsx into the Agents
 * column spec (agents/agent-columns.tsx) when the page adopted
 * <DataTablePage>; these guards are about the page's behaviour, so
 * they read the whole page + satellites concatenated, mirroring
 * `tests/dashboard_sources.py`'s `agents_page_source()` helper (kept
 * in sync with `AGENTS_SOURCES` there).
 */

import { describe, expect, it } from "vitest"
import { agentsPageSource } from "./support/agents-source"

// All three terminal statuses must appear as exclusions in the
// assigned-tasks filter — otherwise the count keeps over-reporting.
const TERMINAL_STATUSES = ["completed", "cancelled", "failed"]

describe("agents page assigned-count excludes terminal tasks", () => {
  it("the `assignedTasks` filter block excludes tasks whose status is completed/cancelled/failed", () => {
    const src = agentsPageSource()
    // Locate the assignedTasks block and look at a window of source
    // around it.
    const m = src.match(/const\s+assignedTasks\s*=/)
    expect(
      m,
      "expected `const assignedTasks = ...` in the Agents page/satellites — " +
        "did the row-level filter move?",
    ).not.toBeNull()
    // The predicate uses `agentTasks.filter(t => ...)`; check the
    // next ~600 chars include the status exclusions.
    const start = m!.index!
    const window = src.slice(start, start + 600)
    for (const status of TERMINAL_STATUSES) {
      const present = window.includes(`'${status}'`) || window.includes(`"${status}"`)
      expect(
        present,
        `assignedTasks filter does not reference ${JSON.stringify(status)} — ` +
          `completed/cancelled/failed tasks will keep being counted ` +
          `as assigned. Window:\n${window}`,
      ).toBe(true)
    }
  })

  it("`taskStats.assigned` is derived from the filtered list, not a raw un-filtered assigned_to predicate", () => {
    const src = agentsPageSource()
    // The display line `{taskStats.assigned > 0 && \`${taskStats.assigned}
    // assigned\`}` must exist (we don't want a refactor to silently
    // bypass our filter).
    expect(
      src.includes("taskStats.assigned"),
      "taskStats.assigned not found — the row's assigned-count label " +
        "must keep using the filtered count, not recompute from raw " +
        "agentTasks",
    ).toBe(true)
    // Belt-and-braces: every line that filters `t.assigned_to ===`
    // to compute a count for display should sit next to a status
    // exclusion. Find every occurrence and verify nearby context.
    const re = /\.filter\(\s*t\s*=>\s*[^)]*t\.assigned_to/g
    let match: RegExpExecArray | null
    while ((match = re.exec(src)) !== null) {
      const end = match.index + match[0].length
      // Look at the next 400 chars for at least one of the three
      // terminal statuses being excluded.
      const window = src.slice(match.index, end + 400)
      const hasExclusion = TERMINAL_STATUSES.some(
        (s) => window.includes(`'${s}'`) || window.includes(`"${s}"`),
      )
      expect(
        hasExclusion,
        `found a .filter(t => ... t.assigned_to ...) block that ` +
          `does not reference any terminal status — it will ` +
          `over-count finished tasks. Block context:\n${window}`,
      ).toBe(true)
    }
  })
})
