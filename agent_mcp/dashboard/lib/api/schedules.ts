// Schedules resource module — the scheduled-directive (event-loop
// scheduler) type + client methods.

import type { ApiClient } from './client'

// A scheduled_directive row (event-loop scheduler). A recurring
// imperative that fires when the target agent next checks in at-or-after
// the interval. `next_due_at` drives the "next fire" column; `enabled`
// backs the inline toggle; `status` is active | paused | completed.
export interface Schedule {
  directive_id: string
  agent_id: string
  prompt: string
  interval_seconds: number
  next_due_at: string
  enabled: boolean
  status: string
  until_at: string | null
  max_runs: number | null
  run_count: number
  created_at: string
  created_by: string | null
  updated_at: string | null
  updated_by: string | null
}

/**
 * Schedule-scoped client methods bound to a shared request core.
 * Assembled onto the composed client by `createApiClient()`.
 *
 * Scheduled directives (event-loop scheduler). Operator/admin surface
 * behind the Schedules tab; all routes are require_operator_session-
 * gated. The backend reuses the same guardrail-enforcing tool impls the
 * agent surface uses, so the floor/max checks apply here too.
 */
export function schedulesApi(core: ApiClient) {
  return {
    async getSchedules(): Promise<Schedule[]> {
      const res = await core.request<{ schedules: Schedule[] }>('/schedules')
      return res.schedules ?? []
    },

    createSchedule(data: {
      agent_id: string
      prompt: string
      interval_seconds: number
      until?: string | null
      count?: number | null
      run_now?: boolean
    }): Promise<{ success: boolean; directive: Schedule }> {
      return core.request('/schedules', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },

    updateSchedule(directiveId: string, data: {
      prompt?: string
      interval_seconds?: number
      enabled?: boolean
      until?: string | null
      count?: number | null
    }): Promise<{ success: boolean; directive: Schedule }> {
      return core.request(`/schedules/${encodeURIComponent(directiveId)}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      })
    },

    deleteSchedule(directiveId: string): Promise<{ success: boolean; deleted: string }> {
      return core.request(`/schedules/${encodeURIComponent(directiveId)}`, {
        method: 'DELETE',
        body: JSON.stringify({}),
      })
    },
  }
}
