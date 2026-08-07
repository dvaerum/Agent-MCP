/**
 * `transportStatusBadge` (lib/status.ts) — ADR-0021 delivery
 * transport_status → badge label + classes.
 *
 * Companion to status-colors.test.ts: pins the runtime half of the
 * delivery-transport badge helper. Every reported value must resolve to
 * a distinct, non-empty label + class string (so a refactor can't
 * silently start rendering an empty badge), and the null / undefined /
 * unrecognised cases must resolve to `null` so the callers (agents
 * dashboard, mobile list, detail panel) render nothing rather than an
 * empty pill.
 */

import { describe, expect, it } from "vitest"
import { transportStatusBadge } from "@/lib/status"
import type { TransportStatus } from "@/lib/api"

const TRANSPORT_STATUSES: TransportStatus[] = [
  "working",
  "idle",
  "dormant",
  "dead",
]

describe("transportStatusBadge", () => {
  for (const status of TRANSPORT_STATUSES) {
    it(`renders a badge for transport_status "${status}"`, () => {
      const badge = transportStatusBadge(status)
      expect(badge).not.toBeNull()
      expect(badge!.label).toEqual(expect.stringMatching(/\S/))
      expect(badge!.className).toEqual(expect.stringMatching(/\S/))
    })
  }

  it("upper-cases the label to match the presence-badge convention", () => {
    expect(transportStatusBadge("working")!.label).toBe("WORKING")
    expect(transportStatusBadge("idle")!.label).toBe("IDLE")
    expect(transportStatusBadge("dormant")!.label).toBe("DORMANT")
    expect(transportStatusBadge("dead")!.label).toBe("DEAD")
  })

  it("gives each status a distinct visual treatment", () => {
    const classNames = TRANSPORT_STATUSES.map(
      (s) => transportStatusBadge(s)!.className,
    )
    expect(new Set(classNames).size).toBe(TRANSPORT_STATUSES.length)
  })

  it("uses only the dashboard's semantic tokens (no raw colors)", () => {
    for (const status of TRANSPORT_STATUSES) {
      const { className } = transportStatusBadge(status)!
      // Guards against reintroducing raw palette classes (e.g.
      // bg-blue-500) that don't theme correctly in light/dark.
      expect(className).not.toMatch(/-\d{2,3}\b/)
    }
  })

  it("renders nothing (null) when transport_status is null", () => {
    expect(transportStatusBadge(null)).toBeNull()
  })

  it("renders nothing (null) when transport_status is absent", () => {
    expect(transportStatusBadge(undefined)).toBeNull()
  })

  it("renders nothing (null) for an unrecognised value", () => {
    expect(
      transportStatusBadge("bogus" as unknown as TransportStatus),
    ).toBeNull()
  })
})
