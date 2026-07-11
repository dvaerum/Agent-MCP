/**
 * `formatRelative` (lib/utils.ts) — arch-r5 #5.
 *
 * Table-driven coverage for the one canonical relative-time
 * formatter that replaced 5 divergent copies (tasks-dashboard,
 * tasks-mobile-list, agents-dashboard, projects-overview-dashboard,
 * agent-details-panel). Locks in: sub-minute "just now", the
 * Nm/Nh/Nd thresholds, per-call-site `emptyLabel`, and dual input
 * shape (ISO string vs epoch-SECONDS number — projects-overview is
 * the one numeric caller).
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest"
import { formatRelative } from "@/lib/utils"

// Fixed "now" so every case is deterministic regardless of wall clock.
const NOW = new Date("2026-01-15T12:00:00.000Z")

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})

afterEach(() => {
  vi.useRealTimers()
})

function isoSecondsAgo(seconds: number): string {
  return new Date(NOW.getTime() - seconds * 1000).toISOString()
}

describe("formatRelative", () => {
  const cases: Array<{
    name: string
    input: string | number | Date | null | undefined
    opts?: { emptyLabel?: string }
    expected: string
  }> = [
    { name: "0s ago (ISO) -> just now", input: isoSecondsAgo(0), expected: "just now" },
    { name: "59s ago (ISO) -> just now", input: isoSecondsAgo(59), expected: "just now" },
    { name: "90s ago (ISO) -> 1m ago", input: isoSecondsAgo(90), expected: "1m ago" },
    { name: "3599s ago (ISO) -> 59m ago", input: isoSecondsAgo(3599), expected: "59m ago" },
    { name: "3600s ago (ISO) -> 1h ago", input: isoSecondsAgo(3600), expected: "1h ago" },
    { name: "86399s ago (ISO) -> 23h ago", input: isoSecondsAgo(86399), expected: "23h ago" },
    { name: "86400s ago (ISO) -> 1d ago", input: isoSecondsAgo(86400), expected: "1d ago" },
    { name: "3d ago (ISO)", input: isoSecondsAgo(3 * 86400), expected: "3d ago" },
    { name: "Date instance, 90s ago -> 1m ago", input: new Date(NOW.getTime() - 90_000), expected: "1m ago" },
    // Numeric input is epoch SECONDS (projects-overview convention).
    { name: "epoch-seconds 0s ago -> just now", input: Math.floor(NOW.getTime() / 1000), expected: "just now" },
    { name: "epoch-seconds 90s ago -> 1m ago", input: Math.floor(NOW.getTime() / 1000) - 90, expected: "1m ago" },
    { name: "epoch-seconds 1d ago -> 1d ago", input: Math.floor(NOW.getTime() / 1000) - 86400, expected: "1d ago" },
    // Empty values fall back to the per-call-site emptyLabel.
    { name: "null -> default emptyLabel", input: null, expected: "—" },
    { name: "undefined -> default emptyLabel", input: undefined, expected: "—" },
    { name: "empty string -> default emptyLabel", input: "", expected: "—" },
    { name: "null -> custom emptyLabel 'never'", input: null, opts: { emptyLabel: "never" }, expected: "never" },
    { name: "undefined -> custom emptyLabel 'unknown'", input: undefined, opts: { emptyLabel: "unknown" }, expected: "unknown" },
    // Unparseable string input is echoed back rather than swallowed.
    { name: "unparseable string is echoed back", input: "not-a-date", expected: "not-a-date" },
  ]

  for (const { name, input, opts, expected } of cases) {
    it(name, () => {
      expect(formatRelative(input, opts)).toBe(expected)
    })
  }
})
