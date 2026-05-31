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
  last_updated: string
  updated_by: string
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
   * `/agent-mcp/__api/<name>` rather than a `http://host:port/api`
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
    
    // Enhanced CORS configuration
    const fetchOptions: RequestInit = {
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
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
      const response = await fetch(url, {
        ...fetchOptions,
        signal: controller.signal
      })
      
      clearTimeout(timeoutId)

      if (!response.ok) {
        const errorText = await response.text().catch(() => 'Unknown error')
        // Only log non-404 errors
        if (response.status !== 404) {
          console.error(`API Error [${response.status}]:`, errorText)
        }
        throw new Error(`API Error: ${response.status} ${response.statusText}`)
      }
      
      return await response.json()
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
    return this.request('/agents', {
      method: 'POST',
      body: JSON.stringify(data)
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
  // working_directory). Admin-only; backed by POST /api/agents/<id>/edit
  // added alongside the dashboard's per-row Edit icon.
  async editAgent(
    agentId: string,
    updates: { capabilities?: string[]; color?: string; working_directory?: string },
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
    sort_by?: 'key' | 'last_updated' | 'size'
  }): Promise<Memory[]> {
    // Note: This would require implementing MCP tool calls via the backend
    // For now, we'll use the context data from getAllData
    const allData = await this.getAllData()
    return allData.context.map(ctx => ({
      context_key: ctx.context_key,
      value: ctx.value,
      description: ctx.description,
      last_updated: ctx.last_updated,
      updated_by: ctx.updated_by,
      _metadata: {
        size_bytes: JSON.stringify(ctx.value).length,
        size_kb: Math.round(JSON.stringify(ctx.value).length / 1024 * 100) / 100,
        json_valid: true,
        days_old: ctx.last_updated ? Math.floor((Date.now() - new Date(ctx.last_updated).getTime()) / (1000 * 60 * 60 * 24)) : undefined,
        is_stale: ctx.last_updated ? (Date.now() - new Date(ctx.last_updated).getTime()) > (30 * 24 * 60 * 60 * 1000) : false,
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