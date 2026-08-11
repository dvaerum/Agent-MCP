import { create } from 'zustand'
import { Agent, ApiError, Task, apiClient } from '../api'
import type { PromptTemplate, PromptCategory } from '../prompt-book'
import {
  normalizeAgentId,
  selectTasks,
  selectActions,
  TERMINAL_TASK_STATUSES,
} from './selectors'
import type { ActionRecord } from './selectors'

// Re-export the helpers so callers that want to read from the store's
// module path (a habit established by the rest of the lib/stores/
// surface) can do so without learning about the new sibling file.
export { normalizeAgentId, selectTasks, selectActions, TERMINAL_TASK_STATUSES }
export type { TaskCriteria, ActionCriteria, ActionRecord } from './selectors'

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

interface AllData {
  agents: Agent[]
  tasks: Task[]
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
  // Data
  data: AllData | null
  loading: boolean
  error: string | null
  lastFetch: number
  isRefreshing: boolean

  // PF-3: live-update SSE stream health. TRUE while the operator
  // events stream (`lib/mcp-notifications.ts`) is connected and
  // delivering `resources/updated` notifications; FALSE before the
  // first connect and whenever the stream drops / reconnects. The
  // background freshness poll (`startDataStoreAutoRefresh`) consults
  // this: when SSE is healthy it already pushes every mutation within
  // ~300ms, so the redundant 60s interval poll is suppressed and only
  // fires as the fallback while SSE is down. Defaults FALSE so the
  // poll runs until the stream proves itself up.
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
  fetchAllData: (force?: boolean) => Promise<void>
  fetchPromptsCatalog: (force?: boolean) => Promise<void>
  invalidatePromptsCatalog: () => void
  getAgent: (agentId: string) => Agent | undefined
  getAgentTasks: (agentId: string) => Task[]
  getAgentActions: (agentId: string) => ActionRecord[]
  getTask: (taskId: string) => Task | undefined
  getContext: (contextKey: string) => ContextRow | undefined
  // Wave 2 (cleanup-wave-2): ``getAdminToken`` removed. The dashboard
  // authenticates via the operator session cookie now (ADR-0003);
  // no UI surface needs the bare admin token.
  getAgentToken: (agentId: string) => string | undefined
  getAgentTaskAnalysis: (agentId: string) => {
    assignedTasks: Task[]
    workedOnTasks: Task[]
    completedTasks: Task[]
    completionActions: ActionRecord[]
    totalTasks: number
    assignedCount: number
    workedOnCount: number
    completedCount: number
    completionActionCount: number
  }
  updateAgent: (agent: Agent) => void
  updateTask: (task: Task) => void
  // PF-3: flip the SSE-health flag. Called by the operator-events
  // stream lifecycle in `lib/mcp-notifications.ts` — true on a
  // successful connect, false on drop / reconnect / stop.
  setSseHealthy: (healthy: boolean) => void
  refreshData: () => Promise<void>
  shouldDisplayAgent: (agent: Agent) => boolean
  getActiveAgents: () => Agent[]
}

export const useDataStore = create<DataStore>((set, get) => ({
  data: null,
  loading: false,
  error: null,
  lastFetch: 0,
  isRefreshing: false,
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

  fetchAllData: async (force = false) => {
    const state = get()
    
    // Skip if already loading
    if (state.loading || state.isRefreshing) return
    
    // Skip if data is fresh (less than 30 seconds old) unless forced
    const now = Date.now()
    if (!force && state.data && now - state.lastFetch < 30000) return
    
    // Set loading state appropriately
    if (!state.data || force) {
      set({ loading: true, error: null })
    } else {
      set({ isRefreshing: true, error: null })
    }
    
    try {
      // Try the new all-data endpoint first
      let data
      try {
        data = await apiClient.getAllData()
      } catch (err) {
        // Fallback to fetching data from individual endpoints — but
        // ONLY when the bulk endpoint is genuinely missing (HTTP 404
        // from a legacy backend that pre-dates the `/all-data`
        // surface). Any other failure (5xx, network abort,
        // AbortController timeout, …) means the backend is unreachable
        // or slow, NOT that the endpoint is absent — fanning out 4
        // more parallel requests in that case just multiplies the
        // pressure on the router proxy and turns one failed read into
        // a 5-fetch cascade. P005 (2026-06-19): the cascade was the
        // observed symptom when navigating to /agent-mcp/app/<proj>/
        // against a cold-spawning backend — every queued fetch piled
        // up behind the same per-project `_ensure_lock` and aborted
        // client-side at the 30 s timeout. Re-raise non-404 errors so
        // the outer catch records the failure and the UI surfaces an
        // honest error instead of doubling down on the same broken
        // upstream.
        if (!(err instanceof ApiError) || err.status !== 404) {
          throw err
        }
        console.debug('All-data endpoint not available, using fallback...')

        const [agents, tasks, tokens, contextData] = await Promise.all([
          apiClient.getAgents(),
          apiClient.getTasks(),
          apiClient.getTokens(),
          apiClient.getContextData()
        ])
        
        // Merge tokens into agents. Wave 2 (cleanup-wave-2): the
        // Admin pseudo-agent no longer falls back to ``admin_token``
        // (Wave 4 deletes that pseudo-agent entirely; in the
        // meantime the dashboard authenticates via the cookie session).
        const agentsWithTokens = agents.map(agent => {
          const token = tokens.agent_tokens.find(t => t.agent_id === agent.agent_id)?.token
          return { ...agent, auth_token: token }
        })

        data = {
          agents: agentsWithTokens,
          tasks,
          context: contextData,
          actions: [],
          file_metadata: [],
          file_map: {},
          timestamp: new Date().toISOString()
        }
      }
      
      console.debug('Fetched all data:', {
        agents: data.agents?.length || 0,
        tasks: data.tasks?.length || 0,
        context: data.context?.length || 0,
        actions: data.actions?.length || 0
      })
      console.debug('Context data received:', data.context)

      // (No selector memoisation to invalidate any more: PR-W1d
      // dropped the per-selector cache in favour of pure
      // `selectTasks`/`selectActions` helpers. The 30s fetchAllData
      // gate above is the only freshness throttle now.)

      set({
        data, 
        loading: false,
        isRefreshing: false, 
        error: null,
        lastFetch: now
      })
    } catch (error) {
      console.debug('Failed to fetch all data:', error)
      set({ 
        loading: false,
        isRefreshing: false, 
        error: error instanceof Error ? error.message : 'Failed to fetch data'
      })
    }
  },

  getAgent: (agentId: string) => {
    const state = get()
    if (!state.data) return undefined

    // Admin row lives under the capitalised 'Admin' agent_id; both
    // 'Admin' and 'admin' inputs must resolve to it. For other
    // agents, strip the optional 'agent_' prefix. The
    // `normalizeAgentId` helper does the prefix-strip + Admin->admin
    // collapse; we then map 'admin' back to 'Admin' for the lookup.
    const normalized = normalizeAgentId(agentId)
    if (normalized === 'admin') {
      return state.data.agents.find(a => a.agent_id === 'Admin')
    }
    return state.data.agents.find(a => a.agent_id === normalized)
  },

  // Tasks this agent has touched: assigned to them OR they recorded an
  // action against. Composes `selectTasks({ assignedTo })` for the
  // assigned slice, derives the worked-on task-id set from
  // `selectActions`, and uses `selectTasks({ taskIdIn, notAssignedTo })`
  // for the worked-on-but-not-assigned slice. The Admin/admin/prefix
  // dance lives in `matchesAgent` inside selectors.ts.
  //
  // No memoisation: the selector is O(tasks + actions), fetchAllData
  // already gates network refresh at 30s, and zustand re-runs are
  // cheap at this size. The previous `memoize(cacheKey, ...)` wrapper
  // conflated cache plumbing with filter logic.
  //
  // Note: this selector intentionally does NOT filter by status. The
  // dashboard's "currently open" view is computed at the call site
  // (see agents-dashboard.tsx) so callers that need history (e.g.
  // the agent-details panel) keep getting it. Callers that want the
  // PR #130 open-only semantics layer `statusNotIn:
  // TERMINAL_TASK_STATUSES` on top via `selectTasks` themselves.
  getAgentTasks: (agentId: string) => {
    const state = get()
    if (!state.data) return []

    const assignedTasks = selectTasks(state.data.tasks, { assignedTo: agentId })

    const agentActions = selectActions(state.data.actions as ActionRecord[], { agentId })
    const workedOnTaskIds = new Set<string>()
    agentActions.forEach((action) => {
      if (typeof action.task_id === 'string') workedOnTaskIds.add(action.task_id)
    })

    const workedOnTasks = selectTasks(state.data.tasks, {
      taskIdIn: workedOnTaskIds,
      notAssignedTo: agentId,
    })

    // Combine and deduplicate (a task could appear in both slices in
    // pathological data; the original code defended against this).
    const seen = new Set<string>()
    const merged: Task[] = []
    for (const t of [...assignedTasks, ...workedOnTasks]) {
      if (!seen.has(t.task_id)) {
        seen.add(t.task_id)
        merged.push(t)
      }
    }
    return merged
  },

  getAgentActions: (agentId: string) => {
    const state = get()
    if (!state.data) return []
    return selectActions(state.data.actions as ActionRecord[], { agentId })
  },

  getTask: (taskId: string) => {
    const state = get()
    if (!state.data) return undefined
    
    // Strip prefix if present
    const cleanId = taskId.startsWith('task_') ? taskId.substring(5) : taskId
    return state.data.tasks.find(t => t.task_id === cleanId)
  },

  getContext: (contextKey: string) => {
    const state = get()
    if (!state.data) return undefined
    
    // Strip prefix if present
    const cleanKey = contextKey.startsWith('context_') ? contextKey.substring(8) : contextKey
    return (state.data.context as ContextRow[]).find(c => c.context_key === cleanKey)
  },

  // Wave 2 (cleanup-wave-2): ``getAdminToken`` removed. Dashboard
  // mutations rely on the operator session cookie set by /agent-mcp/login.

  getAgentToken: (agentId: string) => {
    const agent = get().getAgent(agentId)
    return agent?.auth_token
  },

  getAgentTaskAnalysis: (agentId: string) => {
    const state = get()
    if (!state.data) return {
      assignedTasks: [],
      workedOnTasks: [],
      completedTasks: [],
      completionActions: [],
      totalTasks: 0,
      assignedCount: 0,
      workedOnCount: 0,
      completedCount: 0,
      completionActionCount: 0
    }

    // `allTasks` here is the union "assigned to this agent OR worked
    // on by this agent" -- see getAgentTasks. We compose selectTasks
    // back over that union to get the disjoint slices.
    const allTasks = get().getAgentTasks(agentId)

    const assignedTasks = selectTasks(allTasks, { assignedTo: agentId })
    const workedOnTasks = selectTasks(allTasks, { notAssignedTo: agentId })
    // 'completed' is the only status this aggregator surfaces today
    // (the dashboard widget that consumes `completedCount` shows
    // strictly-completed work; cancelled/failed go elsewhere).
    const completedTasks = selectTasks(allTasks, { statusIn: ['completed'] })

    // Completion actions for this agent. `selectActions` handles the
    // Admin/admin/prefix dance; we then filter for completion-shaped
    // action types. `action.action_type` can be null for legacy
    // rows, so guard the `.includes()` call.
    const completionActions = selectActions(state.data.actions as ActionRecord[], { agentId })
      .filter((a) => {
        const t = a.action_type
        if (typeof t !== 'string') return false
        return t === 'task_completed' || t === 'complete_task' || t.includes('complet')
      })

    return {
      assignedTasks,
      workedOnTasks,
      completedTasks,
      completionActions,
      totalTasks: allTasks.length,
      assignedCount: assignedTasks.length,
      workedOnCount: workedOnTasks.length,
      completedCount: completedTasks.length,
      completionActionCount: completionActions.length
    }
  },

  updateAgent: (agent: Agent) => {
    const state = get()
    if (!state.data) return
    
    const index = state.data.agents.findIndex(a => a.agent_id === agent.agent_id)
    if (index !== -1) {
      const newAgents = [...state.data.agents]
      newAgents[index] = agent
      set({
        data: {
          ...state.data,
          agents: newAgents
        }
      })
    }
  },

  updateTask: (task: Task) => {
    const state = get()
    if (!state.data) return
    
    const index = state.data.tasks.findIndex(t => t.task_id === task.task_id)
    if (index !== -1) {
      const newTasks = [...state.data.tasks]
      newTasks[index] = task
      set({
        data: {
          ...state.data,
          tasks: newTasks
        }
      })
    }
  },

  setSseHealthy: (healthy: boolean) => {
    // Cheap guard: only touch the store when the value actually flips,
    // so subscribers of the `selectSseHealthy` selector don't re-render
    // on every redundant setter call from the stream lifecycle.
    if (get().sseHealthy !== healthy) set({ sseHealthy: healthy })
  },

  refreshData: async () => {
    // Force refresh
    await get().fetchAllData(true)
  },

  // Agent display predicate.
  //
  // The dashboard used to hide any non-admin agent older than 10
  // minutes without a current_task — the same predicate the
  // (now-removed) auto-cleanup loop used to pick termination targets.
  // That combination silently killed valid worker agents and hid the
  // crime scene. Fix: surface every non-terminated row; rely on the
  // existing status-filter dropdown if users want to hide terminated.
  shouldDisplayAgent: (agent: Agent) => agent.status !== 'terminated',

  getActiveAgents: () => {
    const state = get()
    if (!state.data) return []
    return state.data.agents.filter(agent => get().shouldDisplayAgent(agent))
  }
}))

// -- scoped selectors (PF-4) ---------------------------------------------
//
// Narrow, single-slice selector hooks so a component subscribes to
// EXACTLY the state it renders and re-renders only when THAT slice
// changes — not on every unrelated `set()` anywhere in the store.
//
// The anti-pattern these replace is `const { agents } =
// useDataStore()` (no selector), which subscribes the component to the
// WHOLE store object: a `setSseHealthy` flip, a prompts-catalog load, an
// `isRefreshing` toggle — each re-renders every such consumer. A
// slice-scoped `useDataStore(selectAgents)` re-renders only when the
// agents array reference actually changes.
//
// Stable empty singletons: returning a fresh `[]` from a selector on
// every call defeats zustand's reference equality and forces a re-render
// each time, so the null-data path returns one frozen shared array.
//
// W4-followup(A): migrate the components/ consumers off the whole-store
// subscription to these hooks. Known call-sites to convert (Agent B):
//   - components/dashboard/agents-dashboard.tsx  → useAgents()
//   - components/dashboard/tasks-dashboard.tsx   → useTasks()
//   - components/dashboard/messages-dashboard.tsx (data slice) → useAllData()/useTasks()
//   - components/dashboard/system-graph*.tsx / overview widgets → useAgents()/useTasks()
//   - any `useDataStore((s) => ...)` that pulls loading/error/refreshing
//     → useDataLoading() / useDataError() / useIsRefreshing()
//   - live-update indicators → useSseHealthy()
//   - prompt-book consumers → usePromptsCatalog()/usePromptsCategories()

const EMPTY_AGENTS: readonly Agent[] = Object.freeze([])
const EMPTY_TASKS: readonly Task[] = Object.freeze([])

/** The agents array (stable empty ref when no data is loaded yet). */
export const useAgents = (): readonly Agent[] =>
  useDataStore((s) => s.data?.agents ?? EMPTY_AGENTS)

/** The tasks array (stable empty ref when no data is loaded yet). */
export const useTasks = (): readonly Task[] =>
  useDataStore((s) => s.data?.tasks ?? EMPTY_TASKS)

/** The whole `AllData` envelope (or null). Prefer the narrower
 *  `useAgents` / `useTasks` when a component only needs one slice. */
export const useAllData = (): AllData | null => useDataStore((s) => s.data)

/** Initial-load spinner flag. */
export const useDataLoading = (): boolean => useDataStore((s) => s.loading)

/** Background-refresh-in-flight flag (distinct from the initial load). */
export const useIsRefreshing = (): boolean =>
  useDataStore((s) => s.isRefreshing)

/** Last fetch error message, or null. */
export const useDataError = (): string | null =>
  useDataStore((s) => s.error)

/** PF-3 live-update stream health — drives a "live"/"stale" indicator
 *  without subscribing the component to the data envelope itself. */
export const useSseHealthy = (): boolean => useDataStore((s) => s.sseHealthy)

/** Prompt-book catalogue slices (churn on a different cadence than the
 *  hot data, so scope them separately). */
export const usePromptsCatalog = () =>
  useDataStore((s) => s.promptsCatalog)
export const usePromptsCategories = () =>
  useDataStore((s) => s.promptsCategories)
export const usePromptsCatalogLoading = (): boolean =>
  useDataStore((s) => s.promptsCatalogLoading)

// -- background freshness poll -------------------------------------------

/**
 * Slow safety-net poll behind the live-update SSE stream
 * (`lib/mcp-notifications.ts`). The stream is what makes the dashboard
 * feel live; this tick only covers the case where the stream is down
 * and the operator hasn't noticed. 60s (was 30s) — the stream carries
 * the latency-sensitive updates.
 *
 * Ownership: this used to be a bare `setInterval` fired at module
 * import time with its handle discarded — unstoppable by construction,
 * and re-armed on every module re-evaluation (Next Fast Refresh, a
 * test's `vi.resetModules()`), so instances accumulated. It is now
 * started explicitly by `<McpNotificationsProvider>`, which already
 * owns the live-update lifecycle, and the caller holds the stop.
 * `tests/live-update-timer-leaks.test.ts` pins that.
 */
const AUTO_REFRESH_INTERVAL_MS = 60000
let _autoRefreshTimer: ReturnType<typeof setInterval> | null = null
let _autoRefreshOwners = 0

/**
 * Start the background freshness poll. Idempotent — concurrent callers
 * share one interval, refcounted so the last stop() is the one that
 * clears it. Returns a stop function that is safe to call more than
 * once.
 */
export function startDataStoreAutoRefresh(): () => void {
  _autoRefreshOwners += 1
  if (_autoRefreshTimer === null) {
    _autoRefreshTimer = setInterval(() => {
      const store = useDataStore.getState()
      // PF-3: SSE is the primary freshness path — when its stream is
      // healthy it pushes every mutation within ~300ms, so this
      // interval poll is pure redundant load. Suppress it while SSE is
      // up; fall back to polling only when the stream is down (the
      // exact case this safety net exists for).
      if (store.sseHealthy) return
      // A freshness top-up, not the initial load: nothing to refresh
      // until some page has populated the envelope.
      if (store.data) {
        void store.refreshData()
      }
    }, AUTO_REFRESH_INTERVAL_MS)
  }
  let released = false
  return () => {
    if (released) return
    released = true
    _autoRefreshOwners -= 1
    if (_autoRefreshOwners <= 0 && _autoRefreshTimer !== null) {
      clearInterval(_autoRefreshTimer)
      _autoRefreshTimer = null
      _autoRefreshOwners = 0
    }
  }
}

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

