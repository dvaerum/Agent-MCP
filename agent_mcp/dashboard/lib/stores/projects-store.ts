// Agent-MCP/agent_mcp/dashboard/lib/stores/projects-store.ts
//
// Cross-project overview store (Phase 3.5a). Backed by the router's
// `GET /agent-mcp/__overview` endpoint added in the same phase.
//
// Distinct from `useDataStore` (which fetches a single project's
// agents / tasks / context via `/api/all-data`). The overview lives
// one level up: it lists every registered project on the router with
// status + count aggregates per project. The dashboard's
// `app/page.tsx` picks which store to consume based on the
// `projectContext.isOverview` flag — overview route → this store,
// per-project route → the legacy data store.
//
// Refresh strategy:
//   - Manual fetch on mount.
//   - Refetch on MCP `notifications/resources/updated` (E's PR #81
//     listener); the overview cards reflect agent / task / message
//     count changes within a few seconds without polling.
//   - Manual refresh button on the overview header.
//
// No persist layer — the envelope is small and the router caches it
// for ~3s, so the round-trip on each page load is cheap.

import { create } from 'zustand'

export type ProjectStatus =
  | 'active'
  | 'idle'
  | 'sleeping'
  | 'stopped'
  | 'starting'
  | 'failed'

export interface ProjectAlias {
  name: string
  expires_at: string
}

export interface ProjectOverviewRow {
  name: string
  workspace: string
  status: ProjectStatus
  last_activity_ts: number | null
  agents: number
  tasks: number
  open_messages: number
  alias: ProjectAlias[]
}

export interface OverviewEnvelope {
  projects: ProjectOverviewRow[]
  multi_tenant: boolean
  single_tenant_name?: string | null
}

interface ProjectsStore {
  envelope: OverviewEnvelope | null
  loading: boolean
  error: string | null
  lastFetch: number
  fetchOverview: () => Promise<void>
  reset: () => void
}

const OVERVIEW_ENDPOINT = '/agent-mcp/__overview'

export const useProjectsStore = create<ProjectsStore>((set, get) => ({
  envelope: null,
  loading: false,
  error: null,
  lastFetch: 0,

  fetchOverview: async () => {
    // Coalesce in-flight requests: an MCP notification + a manual
    // click landing in the same tick should only issue one request.
    if (get().loading) return
    set({ loading: true, error: null })
    try {
      const r = await fetch(OVERVIEW_ENDPOINT, { cache: 'no-store' })
      if (!r.ok) {
        throw new Error(`overview endpoint returned HTTP ${r.status}`)
      }
      const body = (await r.json()) as OverviewEnvelope
      set({
        envelope: body,
        loading: false,
        error: null,
        lastFetch: Date.now(),
      })
    } catch (e) {
      set({
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      })
    }
  },

  reset: () =>
    set({ envelope: null, loading: false, error: null, lastFetch: 0 }),
}))
