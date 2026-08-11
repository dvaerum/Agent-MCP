import { create } from 'zustand'
import { apiClient } from '../api'
import type { PromptTemplate, PromptCategory } from '../prompt-book'
import {
  normalizeAgentId,
  selectTasks,
  selectActions,
  TERMINAL_TASK_STATUSES,
} from './selectors'

// Re-export the helpers so callers that want to read from the store's
// module path (a habit established by the rest of the lib/stores/
// surface) can do so without learning about the new sibling file.
export { normalizeAgentId, selectTasks, selectActions, TERMINAL_TASK_STATUSES }
export type { TaskCriteria, ActionCriteria, ActionRecord } from './selectors'

// Wave 6 keystone increment 1 (2026-08-11): the `/all-data` server-cache
// that used to live in THIS store — `data`, `fetchAllData`,
// `refreshData`, the data-derived selectors, and the background poll —
// moved onto TanStack Query (`lib/queries/all-data.ts`, single
// `['all-data', project]` query). This store now owns only the two
// slices that are NOT the bulk envelope:
//
//   1. the prompt-book catalogue (separate endpoint + cadence), and
//   2. the PF-3 `sseHealthy` flag (flipped by the SSE stream lifecycle
//      in `lib/mcp-notifications.ts`; read by the all-data query to gate
//      its fallback poll).
//
// The `AllData` / `ContextRow` interfaces stay declared here (imported
// by the queries module + pinned by tests/wave-2-no-admin-token) so the
// envelope's shape has one home.

/**
 * A raw context row from the `/all-data` REST envelope. Only
 * `context_key` is looked up today; the value + remaining columns are
 * untyped JSON.
 */
export interface ContextRow {
  context_key: string
  value?: unknown
  description?: string
  [key: string]: unknown
}

export interface AllData {
  agents: import('../api').Agent[]
  tasks: import('../api').Task[]
  context: unknown[]
  actions: unknown[]
  file_metadata: unknown[]
  file_map: Record<string, unknown>
  // Wave 2 (cleanup-wave-2): ``admin_token`` is no longer surfaced
  // on this slice. Dashboard mutations authenticate via the operator
  // session cookie (handled inside apiClient.request); the admin
  // bearer fallback is dead for browser callers.
  timestamp: string
}

interface DataStore {
  // PF-3: live-update SSE stream health. TRUE while the operator
  // events stream (`lib/mcp-notifications.ts`) is connected and
  // delivering `resources/updated` notifications; FALSE before the
  // first connect and whenever the stream drops / reconnects. The
  // `/all-data` query (`lib/queries/all-data.ts`) consults this: when
  // SSE is healthy it already pushes every mutation within ~300ms, so
  // the redundant 60s query poll is suppressed and only fires as the
  // fallback while SSE is down. Defaults FALSE so the poll runs until
  // the stream proves itself up.
  sseHealthy: boolean

  // Prompt-book catalogue (PR #67 + dashboard-prompts-from-rest
  // migration). Source of truth lives in agent_mcp/prompts/catalog.json
  // and is served via GET /api/prompts/catalog. The slice is its own
  // shape — not folded into the AllData blob — because the catalogue
  // changes on a different cadence (admin create / update / delete)
  // and the rest of the dashboard's hot data shouldn't churn when a
  // prompt edit lands.
  promptsCatalog: PromptTemplate[] | null
  promptsCategories: PromptCategory[] | null
  promptsCatalogLoading: boolean

  // Actions
  fetchPromptsCatalog: (force?: boolean) => Promise<void>
  invalidatePromptsCatalog: () => void
  // PF-3: flip the SSE-health flag. Called by the operator-events
  // stream lifecycle in `lib/mcp-notifications.ts` — true on a
  // successful connect, false on drop / reconnect / stop.
  setSseHealthy: (healthy: boolean) => void
}

export const useDataStore = create<DataStore>((set, get) => ({
  sseHealthy: false,
  promptsCatalog: null,
  promptsCategories: null,
  promptsCatalogLoading: false,

  // Fetch the prompt-book catalogue from GET /api/prompts/catalog.
  // Skips when a fetch is already in flight or when the catalogue is
  // already loaded (unless force=true). Populates both
  // `promptsCatalog` (the prompts array) and `promptsCategories`
  // (the categories array) since the REST envelope returns them
  // together.
  fetchPromptsCatalog: async (force = false) => {
    const state = get()
    if (state.promptsCatalogLoading) return
    if (!force && state.promptsCatalog !== null) return
    set({ promptsCatalogLoading: true })
    try {
      const envelope = await apiClient.getPromptsCatalog()
      // Normalize `tags` at the store boundary so any drift in the
      // JSON catalogue (a prompt without a `tags` key, a null, etc.)
      // is healed before it reaches the React tree. The 2026-06-17
      // Firefox-MCP click-through caught a catalog.json entry
      // (`event-loop`) that lacked `tags` entirely
      // and threw `TypeError: s.tags is undefined` from the
      // dashboard's direct dereference. catalog.json is now
      // backfilled (layer 1) — this map() is the defense-in-depth
      // layer that prevents future regressions even if the
      // catalogue drifts again.
      set({
        promptsCatalog: (envelope.prompts as PromptTemplate[]).map(p => ({
          ...p,
          tags: p.tags ?? [],
        })),
        promptsCategories: envelope.categories as PromptCategory[],
        promptsCatalogLoading: false,
      })
    } catch (err) {
      console.debug('Failed to fetch prompts catalog:', err)
      set({ promptsCatalogLoading: false })
    }
  },

  // Invalidate the cached catalogue + refetch immediately. Wired
  // from the MCP notification listener (see notifyPromptsListChanged
  // export below) so admin create/update/delete reaches other
  // dashboard tabs within seconds rather than on the next manual
  // reload. Setting `promptsCatalog: null` forces fetchPromptsCatalog
  // to actually hit the network even though the cached value exists.
  invalidatePromptsCatalog: () => {
    set({ promptsCatalog: null, promptsCategories: null })
    void get().fetchPromptsCatalog(true)
  },

  setSseHealthy: (healthy: boolean) => {
    // Cheap guard: only touch the store when the value actually flips,
    // so subscribers of the `selectSseHealthy` selector don't re-render
    // on every redundant setter call from the stream lifecycle.
    if (get().sseHealthy !== healthy) set({ sseHealthy: healthy })
  },
}))

// -- scoped selectors ----------------------------------------------------

/** PF-3 live-update stream health — drives a "live"/"stale" indicator
 *  and gates the `/all-data` query's fallback poll. */
export const useSseHealthy = (): boolean => useDataStore((s) => s.sseHealthy)

/** Prompt-book catalogue slices (churn on a different cadence than the
 *  hot data, so scope them separately). */
export const usePromptsCatalog = () =>
  useDataStore((s) => s.promptsCatalog)
export const usePromptsCategories = () =>
  useDataStore((s) => s.promptsCategories)
export const usePromptsCatalogLoading = (): boolean =>
  useDataStore((s) => s.promptsCatalogLoading)

/**
 * Handler for MCP `notifications/prompts/list_changed`.
 *
 * Wire this from wherever the dashboard consumes its `GET /mcp` SSE
 * stream — when a `notifications/prompts/list_changed` payload
 * arrives, call this function. It invalidates the cached prompt
 * catalogue and triggers an immediate refetch, so a prompt created
 * or edited in one dashboard tab (or via the MCP `prompts/list`
 * surface) is visible in other open tabs within seconds rather
 * than on the next manual reload.
 *
 * Exported separately from the store so the consumer site (a
 * notification dispatcher in lib/api.ts or a sibling) can import
 * without taking a React render dependency on the store.
 */
export function notifyPromptsListChanged(): void {
  useDataStore.getState().invalidatePromptsCatalog()
}
