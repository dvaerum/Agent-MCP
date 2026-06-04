// API client for Agent-MCP backend

export interface Agent {
  agent_id: string
  status: 'pending' | 'running' | 'terminated' | 'failed'
  current_task?: string
  working_directory?: string
  color?: string
  capabilities?: string[]
  created_at: string
  updated_at: string
  terminated_at?: string | null
  auth_token?: string
  // 16-char lowercase hex string identifying the matching tmux
  // session inside Agents-of-Empires for the notification side-
  // channel. Empty/missing = no AoE binding (notifier will fall back
  // to title-match resolution).
  aoe_session_id?: string | null
}

export interface Task {
  task_id: string
  title: string
  description?: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled' | 'failed'
  priority: 'low' | 'medium' | 'high'
  assigned_to?: string
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

export interface GraphNode {
  id: string
  label: string
  group?: 'agent' | 'task' | 'context' | 'file' | 'admin'
  type?: 'agent' | 'task' | 'context' | 'file' | 'admin'
  status?: string
  priority?: string
  assigned_to?: string
  current_task?: string
  metadata?: Record<string, unknown>
  [key: string]: unknown
}

export interface GraphEdge {
  id?: string
  from: string
  to: string
  type?: string
  title?: string
  label?: string
  [key: string]: unknown
}

export interface Memory {
  context_key: string
  value: any
  description?: string
  updated_at: string
  updated_by: string
  // Ownership columns (Phase 7b). Optional on the type because legacy
  // rows pre-migration may still carry NULLs from the backfill window.
  created_at?: string | null
  created_by?: string | null
  _metadata?: {
    size_bytes: number
    size_kb: number
    json_valid: boolean
    days_old?: number
    is_stale: boolean
    is_large: boolean
  }
}

export interface MemoryHealthAnalysis {
  status: 'excellent' | 'good' | 'needs_attention' | 'critical' | 'no_data'
  health_score: number
  total: number
  stale_entries: number
  json_errors: number
  large_entries: number
  issues: string[]
  warnings: string[]
  recommendations: string[]
}

export interface SystemStatus {
  server_running: boolean
  total_agents: number
  active_agents: number
  total_tasks: number
  pending_tasks: number
  completed_tasks: number
  last_updated: string
}

export interface AgentDetails {
  agent: Agent
  token?: string
  tasks?: Task[]
  actions?: Array<{
    timestamp: string
    action_type: string
    task_id?: string
    details?: any
  }>
}

class ApiClient {
  private baseUrl: string
  private suppressErrors: boolean = false

  constructor(baseUrl: string = '') {
    this.baseUrl = baseUrl
  }
  
  // Set whether to suppress connection errors (useful during server discovery)
  setSuppressErrors(suppress: boolean) {
    this.suppressErrors = suppress
  }

  // Dynamic server connection.
  //
  // Convention: `baseUrl` IS the API root, including the `/api`
  // path segment when present. setServer appends `/api` so every
  // other method can just concatenate the endpoint without
  // worrying about where the prefix lives.
  setServer(host: string, port: number) {
    this.baseUrl = `http://${host}:${port}/api`
  }

  /**
   * Set the API root URL directly.
   *
   * Use this instead of `setServer(host, port)` when the caller
   * already knows the absolute or relative URL of the API root
   * (for example, path-prefixed deployments mounted behind a
   * reverse-proxy router where the dashboard fetches resolve via
   * `/agent-mcp/api/<name>` (PR-B renamed from /__api/) rather than
   * a `http://host:port/api`
   * origin).
   *
   * The provided URL should be the API root including any `/api`
   * segment, matching the same convention as `setServer`. Endpoint
   * paths are concatenated to this value directly.
   */
  setBaseUrl(url: string) {
    this.baseUrl = url
  }

  /**
   * Returns the API root URL (includes `/api`, not just the server
   * origin). Callers that build URLs from this should concatenate
   * the endpoint directly without adding `/api/` themselves.
   */
  getServerUrl(): string {
    return this.baseUrl
  }

  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    // Check if a server is connected
    if (!this.baseUrl) {
      throw new Error('NO_SERVER_CONNECTED')
    }

    const url = `${this.baseUrl}${endpoint}`
    
    // Enhanced CORS configuration.
    //
    // PR-A: the strict, version-pinned API media type is required by
    // the router's Accept-header gate (/agent-mcp/api/<name>/*). A
    // plain `application/json` Accept value is rejected with 406. The
    // dashboard is a first-class consumer of the v1 surface, so the
    // gate header is part of every request.
    const fetchOptions: RequestInit = {
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/vnd.agent-mcp.v1+json',
        // Don't set Origin header - let browser handle it automatically
        ...options.headers,
      },
      credentials: 'omit', // Don't include credentials for CORS
      mode: 'cors', // Explicitly set CORS mode
      cache: 'no-cache', // Always get fresh data
      ...options,
    }

    // Add timeout support
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 10000) // 10 second timeout

    try {
      // Transparent cold-start retry. A lazily-spawned backend takes
      // ~10-15s to create its Unix socket (Python import time +
      // lifespan startup); during that window the router's proxy
      // returns 502/503/504. Retrying on 5xx with exponential backoff
      // (200ms, 400ms) lets the first request transparently wait for
      // the backend instead of bubbling an error up to a boundary-
      // level useEffect retry loop (the pattern this refactor
      // replaces — Candidate C, architecture review 2026-06-01).
      //
      // Bounded at 3 attempts (200ms + 400ms = 600ms total backoff
      // budget plus the original request's own timeout). 4xx and
      // non-5xx are not retried.
      //
      // Method gate: only safe (read-only) methods are retried. The
      // original implementation retried EVERY method, which silently
      // double-fired non-idempotent mutations when the backend
      // processed a POST/PATCH/DELETE, committed the side-effect, and
      // then crashed/disconnected returning 502 on the response phase.
      // Concrete bug shapes that caused: createTask → two identical
      // tasks (server-generated task_id, no uniqueness collision to
      // catch the dup); sendMessage → double fan-out; terminateAgent
      // → safe (idempotent server-side, but still pointless retry).
      // 5xx on a mutation must reach the caller's catch handler so the
      // operator sees the error and decides whether to retry manually.
      const method = (
        typeof fetchOptions.method === 'string' ? fetchOptions.method : 'GET'
      ).toUpperCase()
      const isReadOnly = method === 'GET' || method === 'HEAD'
      let response: Response | null = null
      for (let attempt = 0; attempt < 3; attempt++) {
        response = await fetch(url, {
          ...fetchOptions,
          signal: controller.signal
        })
        if (
          isReadOnly &&
          response.status >= 500 &&
          response.status < 600 &&
          attempt < 2
        ) {
          await new Promise(res => setTimeout(res, 200 * 2 ** attempt))
          continue
        }
        break
      }

      clearTimeout(timeoutId)

      // Non-null after the loop: the loop body always assigns `response`
      // on its first iteration, and we either continue (assigning again)
      // or break.
      const r = response as Response

      if (!r.ok) {
        const errorText = await r.text().catch(() => 'Unknown error')
        // Only log non-404 errors
        if (r.status !== 404) {
          console.error(`API Error [${r.status}]:`, errorText)
        }
        throw new Error(`API Error: ${r.status} ${r.statusText}`)
      }

      return await r.json()
    } catch (error) {
      clearTimeout(timeoutId)
      
      // Log errors only in debug mode or for non-connection errors
      if (error instanceof Error) {
        // Only log non-connection errors to console when not suppressing
        if (!this.suppressErrors && !error.message.includes('Failed to fetch') && !error.message.includes('ERR_CONNECTION_REFUSED')) {
          console.error(`Request failed to ${url}:`, {
            name: error.name,
            message: error.message,
            stack: error.stack
          })
        }
        
        if (error.name === 'AbortError') {
          throw new Error('Request timeout')
        }
        
        if (error.message.includes('Failed to fetch')) {
          // Throw a clean error without triggering additional console logs
          const err = new Error(`Network error: Unable to connect to ${this.baseUrl}`)
          // Mark this error as expected to prevent logging
          ;(err as any).isExpected = true
          throw err
        }
      }
      
      throw error
    }
  }

  // System endpoints
  async getSystemStatus(): Promise<SystemStatus> {
    return this.request<SystemStatus>('/status')
  }

  async getGraphData(): Promise<{ nodes: GraphNode[], edges: GraphEdge[] }> {
    return this.request<{ nodes: GraphNode[], edges: GraphEdge[] }>('/graph-data')
  }

  async getTaskTreeData(): Promise<{ nodes: GraphNode[], edges: GraphEdge[] }> {
    return this.request<{ nodes: GraphNode[], edges: GraphEdge[] }>('/task-tree-data')
  }

  // Agent endpoints
  async getAgents(): Promise<Agent[]> {
    return this.request<Agent[]>('/agents')
  }

  async getAgent(agentId: string): Promise<Agent> {
    return this.request<Agent>(`/agents/${agentId}`)
  }

  async getAgentDetails(agentId: string): Promise<AgentDetails> {
    // Get agent basic info
    const agent = await this.getAgent(agentId)
    
    // Get tokens
    const tokens = await this.getTokens()
    const agentToken = tokens.agent_tokens.find(t => t.agent_id === agentId)?.token || 
                       (agentId === 'Admin' ? tokens.admin_token : undefined)
    
    // Get node details which includes actions and related tasks
    const nodeDetails = await this.getNodeDetails(`agent_${agentId}`)
    
    return {
      agent: { ...agent, auth_token: agentToken },
      token: agentToken,
      tasks: (nodeDetails.related?.assigned_tasks as Task[]) || [],
      actions: (nodeDetails.actions as any[]) || []
    }
  }

  async createAgent(data: {
    agent_id: string
    capabilities?: string[]
    working_directory?: string
  }): Promise<{ success: boolean; message: string }> {
    // Admin-only — the backend's verify_token() check requires the
    // admin token in the request body, matching the convention used by
    // terminateAgent / restoreAgent / editAgent / purgeAgent. Pre-fix
    // this method shipped just JSON.stringify(data) with no token,
    // which silently 401'd once a POST handler existed. Combined with
    // the missing POST route on /api/agents (also fixed in the same
    // PR) the Deploy button was completely non-functional.
    const tokens = await this.getTokens()
    return this.request('/agents', {
      method: 'POST',
      body: JSON.stringify({ token: tokens.admin_token, ...data }),
    })
  }

  async terminateAgent(agentId: string): Promise<{ success: boolean; message: string }> {
    // Routes to the dashboard shim that wraps the terminate_agent admin tool.
    const tokens = await this.getTokens()
    return this.request('/terminate-agent', {
      method: 'POST',
      body: JSON.stringify({ token: tokens.admin_token, agent_id: agentId }),
    })
  }

  // Restore + Purge for terminated agents. `restoreAgent` flips the
  // soft-delete back; `getPurgePreview` returns blast-radius counts
  // and samples for the confirmation modal; `purgeAgent` runs the
  // cascade tombstone + DELETE. All admin-only.
  async restoreAgent(
    agentId: string,
  ): Promise<{ success: boolean; agent_id: string; status: string; message: string }> {
    const tokens = await this.getTokens()
    return this.request(`/agents/${encodeURIComponent(agentId)}/restore`, {
      method: 'POST',
      body: JSON.stringify({ token: tokens.admin_token }),
    })
  }

  // editAgent updates the editable agent fields (capabilities, color,
  // working_directory, aoe_session_id). Admin-only; backed by
  // POST /api/agents/<id>/edit added alongside the dashboard's
  // per-row Edit icon. aoe_session_id is a 16-char lowercase hex
  // string (or empty to clear) used by the AoE notification side-
  // channel — see features/aoe_notify.py.
  async editAgent(
    agentId: string,
    updates: {
      capabilities?: string[]
      color?: string
      working_directory?: string
      aoe_session_id?: string
    },
  ): Promise<{ success: boolean; agent_id: string; updated: Record<string, unknown>; message: string }> {
    const tokens = await this.getTokens()
    return this.request(
      `/agents/${encodeURIComponent(agentId)}/edit`,
      {
        method: 'POST',
        body: JSON.stringify({ token: tokens.admin_token, ...updates }),
      },
    )
  }

  // aoeHealth probes the configured Agents-of-Empires instance with
  // the current bearer token. Settings panel uses it to warn when the
  // token has gone stale (AoE rotates the file-sourced token on a
  // schedule). Admin-only.
  async aoeHealth(): Promise<{
    status: 'ok' | 'disabled' | 'unauthorized' | 'unreachable' | 'misconfigured'
    message?: string
    session_count?: number
    base_url?: string
  }> {
    const tokens = await this.getTokens()
    return this.request(`/aoe/health?token=${encodeURIComponent(tokens.admin_token)}`)
  }

  async getPurgePreview(agentId: string): Promise<{
    agent_id: string
    status: string
    tombstone: string
    counts: {
      messages_sent: number
      messages_received: number
      tasks_created: number
      tasks_assigned: number
      agent_actions: number
    }
    samples: {
      messages_sent: Array<{ content: string; timestamp: string }>
      tasks_created: string[]
      tasks_assigned: string[]
    }
  }> {
    const tokens = await this.getTokens()
    const qs = new URLSearchParams({ token: tokens.admin_token }).toString()
    return this.request(
      `/agents/${encodeURIComponent(agentId)}/purge-preview?${qs}`,
    )
  }

  async purgeAgent(agentId: string): Promise<{
    success: boolean
    agent_id: string
    tombstone: string
    counts: Record<string, number>
    message: string
  }> {
    const tokens = await this.getTokens()
    return this.request(
      `/agents/${encodeURIComponent(agentId)}?cascade=true`,
      {
        method: 'DELETE',
        body: JSON.stringify({ token: tokens.admin_token }),
      },
    )
  }

  // Task endpoints
  async getTasks(): Promise<Task[]> {
    return this.request<Task[]>('/tasks')
  }

  async getTask(taskId: string): Promise<Task> {
    return this.request<Task>(`/tasks/${taskId}`)
  }

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
  // Older versions of this client posted to PUT /tasks/<id> which
  // doesn't exist upstream and 405'd; fixed here.
  //
  // The `notes` parameter is a string (server appends a structured
  // note entry with author=admin + timestamp). Distinct from
  // Task.notes which is the stored list of note entries.
  async updateTask(
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
    const tokens = await this.getTokens()
    const body: Record<string, unknown> = {
      token: tokens.admin_token,
      task_id: taskId,
    }
    if (data.status) body.status = data.status
    if (data.title !== undefined) body.title = data.title
    if (data.description !== undefined) body.description = data.description
    if (data.priority) body.priority = data.priority
    // assigned_to=null is meaningful (unassign); pass it through.
    if (data.assigned_to !== undefined) body.assigned_to = data.assigned_to
    if (data.notes) body.notes = data.notes
    return this.request('/update-task-dashboard', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  }

  // Create a new task via POST /api/tasks (added upstream in dvaerum#12).
  // The dashboard's Create Task button uses this; takes title +
  // optional description / priority / assigned_to / parent_task.
  // The fork's endpoint maps title→task_title and description→task_description.
  async createTask(data: {
    title: string
    description?: string
    priority?: 'low' | 'medium' | 'high'
    assigned_to?: string
    parent_task?: string
  }): Promise<{ success: boolean; message: string; task_id?: string }> {
    const tokens = await this.getTokens()
    return this.request('/tasks', {
      method: 'POST',
      body: JSON.stringify({
        token: tokens.admin_token,
        task_title: data.title,
        task_description: data.description ?? '',
        priority: data.priority,
        assigned_to: data.assigned_to,
        parent_task: data.parent_task,
      }),
    })
  }

  // Delete a task via DELETE /api/tasks/<id> (added upstream in dvaerum#12).
  async deleteTask(taskId: string): Promise<{ success: boolean; message: string }> {
    const tokens = await this.getTokens()
    return this.request(`/tasks/${taskId}`, {
      method: 'DELETE',
      body: JSON.stringify({ token: tokens.admin_token }),
    })
  }

  // Token endpoints
  async getTokens(): Promise<{
    admin_token: string
    agent_tokens: Array<{ agent_id: string; token: string }>
  }> {
    return this.request('/tokens')
  }

  // Prompt-book catalog endpoint (PR #67). Source of truth is
  // `agent_mcp/prompts/catalog.json`; the dashboard reads via the
  // zustand `promptsCatalog` slice in lib/stores/data-store.ts which
  // calls this method on app boot and after notifications/prompts/
  // list_changed.
  //
  // Shape mirrors the JSON envelope exactly so callers don't have to
  // re-massage fields. `PromptTemplate` and `PromptCategory` live in
  // @/lib/prompt-book — those are the shared types; only the inlined
  // data was removed when this migration shipped.
  async getPromptsCatalog(): Promise<{
    categories: Array<{ id: string; name: string; description: string; icon: string }>
    prompts: Array<{
      id: string
      title: string
      description: string
      category: string
      template: string
      variables: Array<{ name: string; description: string; placeholder: string; required: boolean }>
      usage: string
      examples?: string[]
      tags: string[]
    }>
  }> {
    return this.request('/prompts/catalog')
  }

  // All data endpoint for caching
  async getAllData(): Promise<{
    agents: Agent[]
    tasks: Task[]
    context: any[]
    actions: any[]
    file_metadata: any[]
    file_map: Record<string, any>
    admin_token: string
    timestamp: string
  }> {
    return this.request('/all-data')
  }

  // Node details endpoint
  async getNodeDetails(nodeId: string): Promise<{
    id: string
    type: string
    data: Record<string, unknown>
    actions: Array<Record<string, unknown>>
    related?: Record<string, unknown>
  }> {
    return this.request(`/node-details?node_id=${encodeURIComponent(nodeId)}`)
  }

  // Memory endpoints
  async getMemories(options?: {
    context_key?: string
    search_query?: string
    show_health_analysis?: boolean
    show_stale_entries?: boolean
    max_results?: number
    sort_by?: 'key' | 'updated_at' | 'size'
  }): Promise<Memory[]> {
    // Note: This would require implementing MCP tool calls via the backend
    // For now, we'll use the context data from getAllData
    const allData = await this.getAllData()
    return allData.context.map(ctx => ({
      context_key: ctx.context_key,
      value: ctx.value,
      description: ctx.description,
      updated_at: ctx.updated_at,
      updated_by: ctx.updated_by,
      created_at: ctx.created_at,
      created_by: ctx.created_by,
      _metadata: {
        size_bytes: JSON.stringify(ctx.value).length,
        size_kb: Math.round(JSON.stringify(ctx.value).length / 1024 * 100) / 100,
        json_valid: true,
        days_old: ctx.updated_at ? Math.floor((Date.now() - new Date(ctx.updated_at).getTime()) / (1000 * 60 * 60 * 24)) : undefined,
        is_stale: ctx.updated_at ? (Date.now() - new Date(ctx.updated_at).getTime()) > (30 * 24 * 60 * 60 * 1000) : false,
        is_large: JSON.stringify(ctx.value).length > 10240
      }
    }))
  }

  async createMemory(data: {
    context_key: string
    context_value: any
    description?: string
    token: string
  }): Promise<{ success: boolean; message: string }> {
    // This would need to be implemented as an MCP tool call
    return this.request('/memories', {
      method: 'POST',
      body: JSON.stringify(data)
    })
  }

  async updateMemory(context_key: string, data: {
    context_value: any
    description?: string
    token: string
  }): Promise<{ success: boolean; message: string }> {
    // This would need to be implemented as an MCP tool call
    return this.request(`/memories/${encodeURIComponent(context_key)}`, {
      method: 'PUT',
      body: JSON.stringify(data)
    })
  }

  async deleteMemory(context_key: string, token: string): Promise<{ success: boolean; message: string }> {
    // This would need to be implemented as an MCP tool call
    return this.request(`/memories/${encodeURIComponent(context_key)}`, {
      method: 'DELETE',
      body: JSON.stringify({ token })
    })
  }

  async getMemoryHealth(token: string): Promise<MemoryHealthAnalysis> {
    // This would need to be implemented as an MCP tool call
    return this.request('/memories/health', {
      method: 'POST',
      body: JSON.stringify({ token, show_health_analysis: true })
    })
  }

  // Real-time updates via Server-Sent Events
  createEventSource(endpoint: string): EventSource {
    return new EventSource(`${this.baseUrl}${endpoint}`)
  }

  // Fetch the project context store (a.k.a. "memories"). Pairs with
  // getAllData() above, which already covers it but may 404 on
  // backends that don't implement the bulk endpoint.
  async getContextData(): Promise<any[]> {
    return this.request<any[]>('/context-data').catch(() => [])
  }

  // Utility methods
  async healthCheck(): Promise<{ status: string; timestamp: string }> {
    return this.request('/health')
  }

  // CORS diagnostic method
  async testCORS(): Promise<boolean> {
    try {
      console.log(`Testing CORS connection to: ${this.baseUrl}`)
      
      // Try a simple OPTIONS request first
      const optionsResponse = await fetch(`${this.baseUrl}/health`, {
        method: 'OPTIONS',
        headers: {
          'Access-Control-Request-Method': 'GET',
          'Access-Control-Request-Headers': 'Content-Type'
        }
      })
      
      console.log('OPTIONS preflight response:', {
        status: optionsResponse.status,
        headers: Object.fromEntries(optionsResponse.headers.entries())
      })
      
      // Try the actual health check
      const healthResponse = await this.healthCheck()
      console.log('Health check successful:', healthResponse)
      
      return true
    } catch (error) {
      console.error('CORS test failed:', error)
      return false
    }
  }
}

// Create singleton instance
export const apiClient = new ApiClient()

// React Query keys
export const queryKeys = {
  systemStatus: ['system', 'status'] as const,
  graphData: ['graph', 'data'] as const,
  taskTreeData: ['task-tree', 'data'] as const,
  agents: ['agents'] as const,
  agent: (id: string) => ['agents', id] as const,
  tasks: ['tasks'] as const,
  task: (id: string) => ['tasks', id] as const,
  memories: ['memories'] as const,
  memory: (key: string) => ['memories', key] as const,
  memoryHealth: ['memories', 'health'] as const,
  tokens: ['tokens'] as const,
  nodeDetails: (id: string) => ['node-details', id] as const,
} as const

// Custom hooks for data fetching
export function usePolling(intervalMs: number = 10000) {
  return {
    refetchInterval: intervalMs,
    refetchIntervalInBackground: true,
    staleTime: intervalMs / 2,
  }
}