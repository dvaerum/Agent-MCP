import { describe, expect, it, beforeEach, afterEach, vi } from "vitest"
import {
  agentsInSchedules,
  filterSchedules,
  formatEndCondition,
  formatInterval,
  formatNextFire,
  sortByNextFire,
  type StatusFilter,
} from "@/lib/schedules"
import type { Schedule } from "@/lib/api"

const NOW = new Date("2026-01-15T12:00:00.000Z")

function sched(over: Partial<Schedule>): Schedule {
  return {
    directive_id: "sd_1",
    agent_id: "alice",
    prompt: "do it",
    interval_seconds: 60,
    next_due_at: "2026-01-15T12:05:00.000Z",
    enabled: true,
    status: "active",
    until_at: null,
    max_runs: null,
    run_count: 0,
    created_at: "2026-01-15T11:00:00.000Z",
    created_by: "op",
    updated_at: null,
    updated_by: null,
    ...over,
  }
}

describe("formatInterval", () => {
  const cases: Array<[number, string]> = [
    [45, "45s"],
    [60, "1m"],
    [300, "5m"],
    [3600, "1h"],
    [7200, "2h"],
    [86400, "1d"],
    [90, "90s"], // no whole-minute divisor → seconds
  ]
  for (const [secs, expected] of cases) {
    it(`${secs}s → ${expected}`, () => {
      expect(formatInterval(secs)).toBe(expected)
    })
  }
})

describe("formatNextFire", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW) })
  afterEach(() => { vi.useRealTimers() })

  it("near-now reads 'now'", () => {
    expect(formatNextFire("2026-01-15T12:00:10.000Z")).toBe("now")
  })
  it("minutes out", () => {
    expect(formatNextFire("2026-01-15T12:04:00.000Z")).toBe("in 4m")
  })
  it("hours out", () => {
    expect(formatNextFire("2026-01-15T14:00:00.000Z")).toBe("in 2h")
  })
  it("days out", () => {
    expect(formatNextFire("2026-01-18T12:00:00.000Z")).toBe("in 3d")
  })
  it("past reads 'overdue'", () => {
    expect(formatNextFire("2026-01-15T11:00:00.000Z")).toBe("overdue")
  })
})

describe("formatEndCondition", () => {
  it("none → ∞", () => {
    expect(formatEndCondition({ until_at: null, max_runs: null })).toBe("∞")
  })
  it("count", () => {
    expect(formatEndCondition({ until_at: null, max_runs: 3 })).toBe("3 runs")
  })
})

describe("filterSchedules", () => {
  const rows = [
    sched({ directive_id: "a", agent_id: "alice", status: "active" }),
    sched({ directive_id: "b", agent_id: "bob", status: "paused" }),
    sched({ directive_id: "c", agent_id: "alice", status: "completed" }),
  ]
  it("by agent", () => {
    expect(filterSchedules(rows, "alice", "all").map((s) => s.directive_id))
      .toEqual(["a", "c"])
  })
  it("by status", () => {
    expect(filterSchedules(rows, "all", "paused" as StatusFilter)
      .map((s) => s.directive_id)).toEqual(["b"])
  })
  it("agent + status", () => {
    expect(filterSchedules(rows, "alice", "completed" as StatusFilter)
      .map((s) => s.directive_id)).toEqual(["c"])
  })
})

describe("agentsInSchedules", () => {
  it("distinct + sorted", () => {
    const rows = [sched({ agent_id: "bob" }), sched({ agent_id: "alice" }),
                  sched({ agent_id: "bob" })]
    expect(agentsInSchedules(rows)).toEqual(["alice", "bob"])
  })
})

describe("sortByNextFire", () => {
  it("enabled before disabled, then soonest next_due first", () => {
    const rows = [
      sched({ directive_id: "late", enabled: true, next_due_at: "2026-01-15T13:00:00Z" }),
      sched({ directive_id: "disabled", enabled: false, next_due_at: "2026-01-15T12:01:00Z" }),
      sched({ directive_id: "soon", enabled: true, next_due_at: "2026-01-15T12:10:00Z" }),
    ]
    expect(sortByNextFire(rows).map((s) => s.directive_id))
      .toEqual(["soon", "late", "disabled"])
  })
})
