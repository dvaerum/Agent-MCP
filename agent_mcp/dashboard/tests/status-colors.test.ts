/**
 * `statusColorClasses` / `priorityColorClasses` (lib/status.ts) —
 * arch-r5 #8.
 *
 * Replaces 4 divergent, disagreeing status/priority palette copies
 * (agent-details-panel, task-details-dialog, node-detail-panel).
 * `STATUS_COLOR_CLASSES` is typed as
 * `Record<Agent['status'] | Task['status'], string>`, so an
 * unhandled status is already a *compile-time* error (this file
 * would fail `tsc`/`next build` if a status were dropped from the
 * map). This test pins the *runtime* half: every status/priority
 * value the app actually emits resolves to a non-empty class string,
 * so a future refactor that swaps the Record for a lookup function
 * with a silent fallback doesn't quietly start rendering unstyled
 * badges.
 */

import { describe, expect, it } from "vitest"
import { statusColorClasses, priorityColorClasses } from "@/lib/status"
import type { Agent, Task } from "@/lib/api"

const AGENT_STATUSES: Agent["status"][] = [
  "pending",
  "running",
  "terminated",
  "failed",
]

const TASK_STATUSES: Task["status"][] = [
  "pending",
  "in_progress",
  "completed",
  "cancelled",
  "failed",
]

const PRIORITIES: Task["priority"][] = ["low", "medium", "high"]

describe("statusColorClasses", () => {
  for (const status of AGENT_STATUSES) {
    it(`maps Agent status "${status}" to a non-empty class string`, () => {
      expect(statusColorClasses(status)).toEqual(expect.stringMatching(/\S/))
    })
  }

  for (const status of TASK_STATUSES) {
    it(`maps Task status "${status}" to a non-empty class string`, () => {
      expect(statusColorClasses(status)).toEqual(expect.stringMatching(/\S/))
    })
  }

  it("gives 'pending' the same color for both Agent and Task (shared vocabulary)", () => {
    expect(statusColorClasses("pending" as Agent["status"])).toBe(
      statusColorClasses("pending" as Task["status"]),
    )
  })

  it("gives 'failed' the same color for both Agent and Task (shared vocabulary)", () => {
    expect(statusColorClasses("failed" as Agent["status"])).toBe(
      statusColorClasses("failed" as Task["status"]),
    )
  })
})

describe("priorityColorClasses", () => {
  for (const priority of PRIORITIES) {
    it(`maps priority "${priority}" to a non-empty class string`, () => {
      expect(priorityColorClasses(priority)).toEqual(expect.stringMatching(/\S/))
    })
  }
})
