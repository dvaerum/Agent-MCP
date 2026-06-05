/**
 * Composable selectors for the dashboard data-store.
 *
 * Extracted in PR-W1d (architecture deepening, Finding #7) so the
 * Admin/admin/strip-prefix dance plus the `t.assigned_to ===` predicate
 * have exactly one home. Before this file, three Zustand selectors --
 * `getAgentTasks`, `getAgentActions`, `getAgentTaskAnalysis` -- each
 * inlined the same matching logic; the dashboard's PR #130 fix
 * ("completed tasks shown as assigned") only landed in the row pill
 * inside agents-dashboard.tsx, leaving the data-store duplicates
 * silently buggy. Composing through `selectTasks({ assignedTo,
 * statusNotIn, ... })` makes the next fix apply in one place.
 *
 * No backend changes; no SWR adoption (out of scope per the plan).
 */

import type { Task } from '../api'

/**
 * Normalise an agent id to the form stored in `Task.assigned_to`.
 *
 * Two rules, in order:
 *
 *   1. Strip the `agent_` prefix if present. Callers sometimes use
 *      `agent_<id>` (the URL-routing convention) and sometimes the
 *      raw `<id>` (what comes back in /api/all-data). The store
 *      should accept both.
 *   2. Map `Admin` -> `admin`. The system has historically written
 *      both casings to the DB; the canonical form for matching is
 *      lowercase `admin`. Callers that supply `Admin` are expecting
 *      to match `admin` tasks too.
 *
 * The function intentionally returns a single string, not a pair.
 * Predicates that need to match both casings (see `matchesAgent`
 * below) build the disjunction from `normalizeAgentId`'s output and
 * the original input.
 */
export function normalizeAgentId(agentId: string): string {
  const stripped = agentId.startsWith('agent_') ? agentId.substring(6) : agentId
  return stripped === 'Admin' ? 'admin' : stripped
}

/**
 * Compose a predicate that returns true when `candidate` refers to
 * the same agent as `agentId`, tolerating Admin/admin casing drift
 * and the optional `agent_` prefix.
 *
 * Exported because both `selectTasks` (via `assignedTo`) and
 * `selectActions` need the same disjunction.
 */
export function matchesAgent(candidate: string | null | undefined, agentId: string): boolean {
  if (!candidate) return false
  const cleanAgentId = agentId.startsWith('agent_') ? agentId.substring(6) : agentId
  const normalizedAgentId = normalizeAgentId(agentId)
  if (candidate === normalizedAgentId) return true
  if (candidate === cleanAgentId) return true
  // Admin/admin both-ways: when the caller asked about admin, accept
  // either casing on the candidate side too.
  if (normalizedAgentId === 'admin' && (candidate === 'Admin' || candidate === 'admin')) {
    return true
  }
  return false
}

/**
 * Composable filter criteria for `selectTasks`. All fields are
 * optional and AND together. Add fields here as new selectors grow
 * (e.g. `priorityIn`, `createdAfter`) so the helper stays the single
 * filter seam.
 */
export type TaskCriteria = {
  /**
   * Restrict to tasks whose `assigned_to` matches `assignedTo`
   * (tolerating Admin/admin casing and the `agent_` prefix; see
   * `matchesAgent`).
   */
  assignedTo?: string

  /**
   * Restrict to tasks whose `assigned_to` does NOT match
   * `notAssignedTo`. Used by `getAgentTaskAnalysis` to compute the
   * "worked on but not assigned" slice.
   */
  notAssignedTo?: string

  /**
   * Restrict to tasks whose `status` is in this list. Mutually
   * usable with `statusNotIn` (both AND together if both are given).
   */
  statusIn?: string[]

  /**
   * Restrict to tasks whose `status` is NOT in this list. The PR #130
   * fix uses `statusNotIn: ['completed', 'cancelled', 'failed']` to
   * mean "still on the agent's plate".
   */
  statusNotIn?: string[]

  /**
   * Restrict to tasks whose `task_id` is in this set. Used to fold
   * an action-derived task-id set (`workedOnTaskIds`) into the
   * `selectTasks` pipeline.
   */
  taskIdIn?: Set<string>
}

/**
 * Filter `tasks` by `criteria`. Returns a fresh array. Order is
 * preserved from the input.
 *
 * No memoisation -- the three callers are each O(tasks) and the data
 * store already gates fetches at 30s+ intervals, so re-running per
 * render is fine. (The old `memoize(key, ...)` cache in data-store.ts
 * conflated cache-key construction with filter logic; this helper
 * stays a pure function so callers can decide whether to wrap.)
 */
export function selectTasks(tasks: Task[], criteria: TaskCriteria): Task[] {
  const { assignedTo, notAssignedTo, statusIn, statusNotIn, taskIdIn } = criteria

  return tasks.filter((t) => {
    if (assignedTo !== undefined && !matchesAgent(t.assigned_to, assignedTo)) {
      return false
    }
    if (notAssignedTo !== undefined && matchesAgent(t.assigned_to, notAssignedTo)) {
      return false
    }
    if (statusIn !== undefined && !statusIn.includes(t.status)) {
      return false
    }
    if (statusNotIn !== undefined && statusNotIn.includes(t.status)) {
      return false
    }
    if (taskIdIn !== undefined && !taskIdIn.has(t.task_id)) {
      return false
    }
    return true
  })
}

/**
 * The three terminal task statuses the dashboard treats as "not on
 * the agent's plate any more". Lifted from the PR #130 fix in
 * agents-dashboard.tsx so the same constant feeds the data-store
 * selectors that need to honour the same invariant.
 */
export const TERMINAL_TASK_STATUSES: readonly string[] = ['completed', 'cancelled', 'failed']

/**
 * Composable filter criteria for actions. Mirrors `TaskCriteria` but
 * for the `actions` slice; kept minimal because only one selector
 * (`getAgentActions`) consumes it today.
 */
export type ActionCriteria = {
  /** Match actions whose `agent_id` refers to `agentId`. */
  agentId?: string
}

/**
 * Filter actions by `criteria`. Same shape as `selectTasks`. Typed
 * loose (`any[]`) because the dashboard's action records are not
 * statically typed yet -- the all-data REST envelope returns the raw
 * `agent_actions` rows with no TS interface.
 */
export function selectActions(actions: any[], criteria: ActionCriteria): any[] {
  const { agentId } = criteria
  return actions.filter((a) => {
    if (agentId !== undefined && !matchesAgent(a.agent_id, agentId)) return false
    return true
  })
}
