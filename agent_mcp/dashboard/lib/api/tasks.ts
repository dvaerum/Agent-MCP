// Tasks resource module — types, wire-normalization helpers, the
// query-string builder, the raw-task shape guards, and the task-scoped
// client methods.

import type { ApiClient } from './client'
import { ShapeError, isRecord, describe } from './client'

export interface Task {
  task_id: string
  title: string
  description?: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled' | 'failed'
  priority: 'low' | 'medium' | 'high'
  assigned_to?: string
  created_by?: string
  parent_task?: string
  child_tasks?: string[]
  depends_on_tasks?: string[]
  notes?: Array<{
    timestamp: string
    author: string
    content: string
  }>
  created_at: string
  updated_at: string
}

/**
 * A task row exactly as it arrives from the backend, BEFORE the
 * lib/api boundary normalizes it.
 *
 * TY-2: `child_tasks` / `depends_on_tasks` are polymorphic on the
 * wire. The `/all-data` + `/tasks` envelopes serialize them as
 * JSON-encoded strings (see `TaskMirror.child_tasks: string | null`
 * in api-types.generated.ts), but some code paths / already-normalized
 * callers hand back a real `string[]`, and legacy/empty rows send
 * `null`. Rather than leak that `string | string[] | null` union to
 * every consumer (which forced each component to carry its own
 * `parseJsonField` defensive parse), `normalizeTask` collapses both
 * fields to a single shape — `string[]` — at the boundary. Consumers
 * then get exactly one type (`Task`, whose `child_tasks?: string[]`).
 */
export interface RawTask
  extends Omit<Task, 'child_tasks' | 'depends_on_tasks'> {
  child_tasks?: string | string[] | null
  depends_on_tasks?: string | string[] | null
}

/**
 * Collapse a polymorphic task list-field (`child_tasks` /
 * `depends_on_tasks`) to a `string[]`.
 *
 * Accepts the three shapes the backend actually emits:
 *   - `string[]`         → filtered to the string members, returned as-is
 *   - JSON-array string  → parsed (`'["a","b"]'` → `['a','b']`)
 *   - anything else       → `[]` (null, undefined, empty string, a
 *                            non-JSON string, a JSON non-array)
 *
 * Mirrors the defensive `parseJsonField` that component code grew
 * independently; hoisting it to the boundary lets those call-sites drop
 * their copies (see W4-followup notes).
 */
export function normalizeTaskListField(field: unknown): string[] {
  if (Array.isArray(field)) {
    return field.filter((x): x is string => typeof x === 'string')
  }
  if (typeof field === 'string') {
    try {
      const parsed = JSON.parse(field)
      return Array.isArray(parsed)
        ? parsed.filter((x): x is string => typeof x === 'string')
        : []
    } catch {
      return []
    }
  }
  return []
}

/**
 * Normalize a raw task row into the canonical `Task` shape: both
 * list-fields become `string[]` regardless of how the backend encoded
 * them. Idempotent — a `Task` that already carries arrays passes
 * through unchanged.
 */
export function normalizeTask(raw: RawTask): Task {
  return {
    ...raw,
    child_tasks: normalizeTaskListField(raw.child_tasks),
    depends_on_tasks: normalizeTaskListField(raw.depends_on_tasks),
  }
}

/**
 * Optional server-side filters for `GET /tasks`. All fields are
 * optional and AND-combined by the backend (the single source of
 * truth shared with the MCP `view_tasks` tool). Mirrors the REST
 * contract:
 *
 *   - `status`      — one of the task statuses, or the `incomplete`
 *                     alias (a.k.a. `active`/`open`) meaning every
 *                     non-terminal task (pending + in_progress).
 *   - `assigned_to` — exact assignee agent_id.
 *   - `unassigned`  — the claimable pool (tasks with no assignee).
 *   - `assigned`    — complement of `unassigned` (tasks WITH an
 *                     assignee).
 *   - `created_by`  — tasks filed by that agent.
 *
 * Collision-safety: assignment is expressed via the dedicated
 * `assigned` / `unassigned` booleans, never a magic
 * `assigned_to="unassigned"` sentinel — so an agent literally named
 * "unassigned" can't be confused with the claimable-pool filter.
 */
export interface TaskFilters {
  status?: string
  assigned?: boolean
  unassigned?: boolean
  assigned_to?: string
  created_by?: string
}

/**
 * Serialize `TaskFilters` into a query string (including the leading
 * `?`) for `GET /tasks`. Falsy / empty values are omitted; the
 * `assigned` / `unassigned` booleans serialize as `true` only when
 * set (never `false`). Returns `''` when there is nothing to filter,
 * so `getTasks()` stays byte-for-byte back-compatible.
 *
 * Exported for unit testing.
 */
export function buildTasksQuery(filters?: TaskFilters): string {
  if (!filters) return ''
  const params = new URLSearchParams()
  if (filters.status) params.set('status', filters.status)
  if (filters.assigned_to) params.set('assigned_to', filters.assigned_to)
  if (filters.created_by) params.set('created_by', filters.created_by)
  // Dedicated booleans — never emit a magic assigned_to="unassigned"
  // value that an agent named "unassigned" could collide with.
  if (filters.assigned) params.set('assigned', 'true')
  if (filters.unassigned) params.set('unassigned', 'true')
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

/**
 * Thin runtime shape guards used at the `request<T>()` boundary for the
 * task read paths. Deliberately NOT a full schema library — the ORM
 * invariant already pins the row shapes, so these only catch the gross
 * mismatches a bare `as T` would silently wave through.
 */
export const taskGuards = {
  rawTaskArray(data: unknown): RawTask[] {
    if (!Array.isArray(data)) {
      throw new ShapeError(
        `GET /tasks: expected a Task[] array, got ${describe(data)}`,
      )
    }
    for (const t of data) {
      if (!isRecord(t) || typeof t.task_id !== 'string') {
        throw new ShapeError(
          `GET /tasks: array member is not a Task (missing string ` +
            `task_id): ${describe(t)}`,
        )
      }
    }
    return data as unknown as RawTask[]
  },

  rawTask(data: unknown): RawTask {
    if (!isRecord(data) || typeof data.task_id !== 'string') {
      throw new ShapeError(
        `GET /tasks/<id>: expected a Task object with a string ` +
          `task_id, got ${describe(data)}`,
      )
    }
    return data as unknown as RawTask
  },
}

/**
 * Task-scoped client methods bound to a shared request core. Assembled
 * onto the composed client by `createApiClient()`.
 */
export function tasksApi(core: ApiClient) {
  return {
    // Task endpoints
    //
    // Optional server-side filters (status / assignment / creator) drive
    // GET /tasks — the single source of truth shared with the backend +
    // the MCP `view_tasks` tool. Called with no args it stays a plain
    // `GET /tasks` (back-compat). See `buildTasksQuery` / `TaskFilters`.
    async getTasks(filters?: TaskFilters): Promise<Task[]> {
      // TY-1 guard on the raw rows, then TY-2 normalize child_tasks /
      // depends_on_tasks to arrays so every consumer sees one shape.
      const raw = await core.request<RawTask[]>(
        `/tasks${buildTasksQuery(filters)}`,
        {},
        taskGuards.rawTaskArray,
      )
      return raw.map(normalizeTask)
    },

    async getTask(taskId: string): Promise<Task> {
      const raw = await core.request<RawTask>(
        `/tasks/${taskId}`,
        {},
        taskGuards.rawTask,
      )
      return normalizeTask(raw)
    },

    // Update an existing task. Upstream exposes this as
    // POST /api/update-task-dashboard which takes the admin token + the
    // task_id + the fields to change in the JSON body. Supported keys:
    // - status:       pending | in_progress | completed | cancelled | failed
    // - title:        string
    // - description:  string
    // - priority:     low | medium | high
    // - assigned_to:  agent_id string, or null/'' to clear assignment
    // - notes:        free text — appended as a new note entry by the backend
    //
    // At least one editable field must be provided (otherwise the
    // backend returns 400 — a no-op write is rejected).
    //
    // The `notes` parameter is a string (server appends a structured
    // note entry with author=admin + timestamp). Distinct from
    // Task.notes which is the stored list of note entries.
    updateTask(
      taskId: string,
      data: {
        status?: Task['status']
        title?: string
        description?: string
        priority?: Task['priority']
        assigned_to?: string | null
        notes?: string
      }
    ): Promise<{ success: boolean; message: string }> {
      // PR D (prancy-napping-pie): cookie auth, no body-token field.
      const body: Record<string, unknown> = { task_id: taskId }
      if (data.status) body.status = data.status
      if (data.title !== undefined) body.title = data.title
      if (data.description !== undefined) body.description = data.description
      if (data.priority) body.priority = data.priority
      // assigned_to=null is meaningful (unassign); pass it through.
      if (data.assigned_to !== undefined) body.assigned_to = data.assigned_to
      if (data.notes) body.notes = data.notes
      return core.request('/update-task-dashboard', {
        method: 'POST',
        body: JSON.stringify(body),
      })
    },

    // Create a new task via POST /api/tasks (added upstream in dvaerum#12).
    // The dashboard's Create Task button uses this; takes title +
    // optional description / priority / assigned_to / parent_task.
    // The fork's endpoint maps title→task_title and description→task_description.
    createTask(data: {
      title: string
      description?: string
      priority?: 'low' | 'medium' | 'high'
      assigned_to?: string
      parent_task?: string
    }): Promise<{ success: boolean; message: string; task_id?: string }> {
      // PR D (prancy-napping-pie): cookie auth, no body-token field.
      return core.request('/tasks', {
        method: 'POST',
        body: JSON.stringify({
          task_title: data.title,
          task_description: data.description ?? '',
          priority: data.priority,
          assigned_to: data.assigned_to,
          parent_task: data.parent_task,
        }),
      })
    },

    // Blast radius of a task delete — the descendant subtree plus the two
    // other conditions the backend refuses on (dependents / an agent's
    // current_task). Mirrors `getPurgePreview`. The delete dialog reads
    // `requires_force` to choose its confirmation tier.
    getTaskDeletePreview(taskId: string): Promise<{
      task_id: string
      title: string
      descendant_count: number
      descendants: Array<{
        task_id: string
        title: string
        status: string
        assigned_to: string | null
      }>
      dependent_count: number
      dependents: Array<{ task_id: string; title: string }>
      blocking_agents: string[]
      requires_force: boolean
    }> {
      return core.request(
        `/tasks/${encodeURIComponent(taskId)}/delete-preview`,
      )
    },

    // Delete a task via DELETE /api/tasks/<id> (added upstream in dvaerum#12).
    // PR D: cookie auth.
    //
    // `force` is the operator's EXPLICIT cascade confirmation and defaults
    // to false. The route used to hardcode `force_delete=true` server-side,
    // which made the backend's cascade guard dead code on this surface: one
    // click deleted a whole descendant subtree. Only the tier-2 branch of
    // the delete dialog — the one that showed the count and made the
    // operator type DELETE — may pass true.
    deleteTask(
      taskId: string,
      opts?: { force?: boolean },
    ): Promise<{ success: boolean; message: string }> {
      return core.request(`/tasks/${taskId}`, {
        method: 'DELETE',
        body: JSON.stringify({ force_delete: opts?.force === true }),
      })
    },
  }
}
