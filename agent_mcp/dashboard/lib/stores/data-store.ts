import { create } from 'zustand'
import { Agent, ApiError, Task, apiClient } from '../api'
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
export type { TaskCriteria, ActionCriteria } from './selectors'

// Debounce utility for API calls
function debounce<T extends (...args: any[]) => any>(
  func: T,
  wait: number
): (...args: Parameters<T>) => void {
  let timeout: NodeJS.Timeout | null = null
  
  return (...args: Parameters<T>) => {
    if (timeout) {
      clearTimeout(timeout)
    }
    
    timeout = setTimeout(() => {
      func(...args)
    }, wait)
  }
}

interface AllData {
  agents: Agent[]
  tasks: Task[]
  context: any[]
  actions: any[]
  file_metadata: any[]
  file_map: Record<string, any>
  admin_token: string
  timestamp: string
}

interface DataStore {
  // Data
  data: AllData | null
  loading: boolean
  error: string | null
  lastFetch: number
  isRefreshing: boolean

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
  getAgentActions: (agentId: string) => any[]
  getTask: (taskId: string) => Task | undefined
  getContext: (contextKey: string) => any | undefined
  getAdminToken: () => string | undefined
  getAgentToken: (agentId: string) => string | undefined
  getAgentTaskAnalysis: (agentId: string) => {
    assignedTasks: Task[]
    workedOnTasks: Task[]
    completedTasks: Task[]
    completionActions: any[]
    totalTasks: number
    assignedCount: number
    workedOnCount: number
    completedCount: number
    completionActionCount: number
  }
  updateAgent: (agent: Agent) => void
  updateTask: (task: Task) => void
  refreshData: () => Promise<void>
  shouldDisplayAgent: (agent: any) => boolean
  getActiveAgents: () => any[]
}

export const useDataStore = create<DataStore>((set, get) => ({
  data: null,
  loading: false,
  error: null,
  lastFetch: 0,
  isRefreshing: false,
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
      // (`agent-mcp-enter-event-loop`) that lacked `tags` entirely
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
        
        // Merge tokens into agents
        const agentsWithTokens = agents.map(agent => {
          const token = tokens.agent_tokens.find(t => t.agent_id === agent.agent_id)?.token ||
                        (agent.agent_id === 'Admin' ? tokens.admin_token : undefined)
          return { ...agent, auth_token: token }
        })
        
        data = {
          agents: agentsWithTokens,
          tasks,
          context: contextData,
          actions: [],
          file_metadata: [],
          file_map: {},
          admin_token: tokens.admin_token,
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

    const agentActions = selectActions(state.data.actions, { agentId })
    const workedOnTaskIds = new Set<string>()
    agentActions.forEach((action) => {
      if (action.task_id) workedOnTaskIds.add(action.task_id)
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
    return selectActions(state.data.actions, { agentId })
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
    return state.data.context.find(c => c.context_key === cleanKey)
  },

  getAdminToken: () => {
    const state = get()
    return state.data?.admin_token
  },

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
    const completionActions = selectActions(state.data.actions, { agentId })
      .filter((a) => {
        const t = a.action_type
        if (!t) return false
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

  refreshData: async () => {
    // Force refresh
    await get().fetchAllData(true)
  },
  
  // Debounced refresh to prevent rapid successive calls
  debouncedRefresh: debounce(async () => {
    await get().fetchAllData()
  }, 500),

  // Agent display predicate.
  //
  // The dashboard used to hide any non-admin agent older than 10
  // minutes without a current_task — the same predicate the
  // (now-removed) auto-cleanup loop used to pick termination targets.
  // That combination silently killed valid worker agents and hid the
  // crime scene. Fix: surface every non-terminated row; rely on the
  // existing status-filter dropdown if users want to hide terminated.
  shouldDisplayAgent: (agent: any) => agent.status !== 'terminated',

  getActiveAgents: () => {
    const state = get()
    if (!state.data) return []
    return state.data.agents.filter(agent => get().shouldDisplayAgent(agent))
  }
}))

// Auto-refresh every 60 seconds (reduced from 30s for better performance)
if (typeof window !== 'undefined') {
  setInterval(() => {
    const store = useDataStore.getState()
    if (store.data) {
      store.refreshData()
    }
  }, 60000)
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

