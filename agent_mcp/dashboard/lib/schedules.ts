// Pure helpers for the Schedules dashboard (event-loop scheduler).
// Extracted so the formatting + grouping logic is unit-testable without
// mounting the React tree (mirrors settings-dashboard's exported helpers).

import type { Schedule } from "@/lib/api"

/**
 * Human interval label from a duration in seconds: "45s", "5m", "2h",
 * "3d". Picks the largest whole unit that divides evenly, else falls back
 * to the next-smaller unit with a rounded value.
 */
export function formatInterval(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0s"
  const units: Array<[number, string]> = [
    [86400, "d"],
    [3600, "h"],
    [60, "m"],
    [1, "s"],
  ]
  for (const [size, suffix] of units) {
    if (seconds % size === 0) return `${seconds / size}${suffix}`
  }
  // Non-even: express in the largest unit that gives >= 1, rounded.
  for (const [size, suffix] of units) {
    if (seconds >= size) return `${Math.round(seconds / size)}${suffix}`
  }
  return `${seconds}s`
}

/**
 * Relative "next fire" label for a FUTURE timestamp: "now" (due within a
 * few seconds), "in 4m", "in 2h", "in 3d", or "overdue" when the fire is
 * in the past (an offline agent will get it on reconnect). Deterministic —
 * pass `now` in tests.
 */
export function formatNextFire(nextDueAt: string, now: Date = new Date()): string {
  const due = new Date(nextDueAt).getTime()
  if (Number.isNaN(due)) return "—"
  const deltaMs = due - now.getTime()
  const deltaS = Math.round(deltaMs / 1000)
  if (deltaS <= -2) return "overdue"
  if (deltaS < 30) return "now"
  if (deltaS < 3600) return `in ${Math.round(deltaS / 60)}m`
  if (deltaS < 86400) return `in ${Math.round(deltaS / 3600)}h`
  return `in ${Math.round(deltaS / 86400)}d`
}

/** Absolute local datetime for the "next fire" tooltip. */
export function formatAbsolute(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

/** Human end-condition summary for the table: "until …", "3 runs", or "∞". */
export function formatEndCondition(s: Pick<Schedule, "until_at" | "max_runs">): string {
  const parts: string[] = []
  if (s.until_at) parts.push(`until ${formatAbsolute(s.until_at)}`)
  if (s.max_runs != null) parts.push(`${s.max_runs} runs`)
  return parts.length ? parts.join(", ") : "∞"
}

export type StatusFilter = "all" | "active" | "paused" | "completed"

/** Filter by agent (or all) and status (or all). */
export function filterSchedules(
  schedules: readonly Schedule[],
  agentFilter: string,
  statusFilter: StatusFilter,
): Schedule[] {
  return schedules.filter((s) => {
    if (agentFilter !== "all" && s.agent_id !== agentFilter) return false
    if (statusFilter !== "all" && s.status !== statusFilter) return false
    return true
  })
}

/** Distinct agent ids present in the schedule set, sorted. */
export function agentsInSchedules(schedules: readonly Schedule[]): string[] {
  return Array.from(new Set(schedules.map((s) => s.agent_id))).sort()
}

/**
 * Sort by soonest next fire first (active schedules the operator cares
 * about float up); completed/paused keep their next_due ordering but a
 * disabled row sorts after an enabled one at the same time.
 */
export function sortByNextFire(schedules: Schedule[]): Schedule[] {
  return [...schedules].sort((a, b) => {
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
    return a.next_due_at.localeCompare(b.next_due_at)
  })
}
