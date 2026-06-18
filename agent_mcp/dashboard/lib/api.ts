// API client for Agent-MCP backend
//
// PR-W3 (ORM big-bang, v5.0.19): the canonical row shapes for every
// persistent table live in `./api-types.generated.ts`, emitted by
// `scripts/generate_ts_types.py` from the Pydantic mirrors in
// `agent_mcp/db/pydantic_mirrors.py`. New dashboard code should
// prefer those interfaces (suffixed `Mirror`) because they are
// guaranteed to stay column-accurate with the ORM via the CI
// invariant in tests/test_orm_is_source_of_truth.py.
//
// The hand-maintained `Agent` / `Task` / `Memory` etc interfaces
// declared below are kept for back-compat. They add richer literal
// unions (status: 'pending' | 'running' | ...) and structured array
// types (capabilities: string[]) the bare DB column types can't
// express. As callers migrate to the generated types, the manual
// declarations here will get trimmed.
export * from './api-types.generated'

import { loginUrl } from './urls'

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
  // Event-coord PR-1: per-agent wake-loop toggle. Default TRUE for
  // every existing row (backfilled via 0010 migration `DEFAULT 1`).
  // When FALSE, the PR-2 `serverInfo.instructions` wake-loop
  // bootstrap is omitted for this agent regardless of the global
  // flag. Greyed out in the edit modal when global is OFF.
  auto_event_loop?: boolean
  // Event-coord PR-1: ISO cursor for `fetch_events_since` (PR-2).
  // NULL until the agent first drains its catch-up window.
  last_event_seen_at?: string | null
  // Event-coord PR-3: TRUE while the agent currently has an
  // in-flight `wait_for_events` long-poll call (i.e. while its
  // `g.lock_for(agent_id)` is held). Drives the "waiting" chip on
  // the Agents table and the "X agents currently in wait" count on
  // the Settings page. Always FALSE for the synthetic Admin row.
  wait_for_events_in_flight?: boolean
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
  // Event-coord PR-1: JSON-encoded list of lowercase capability
  // labels (the server stores it as TEXT and the /api endpoints
  // currently return the raw column). The dashboard parses it lazily
  // for the task-detail view; for routing decisions it's always
  // already-normalized lowercase strings.
  required_capabilities?: string | string[] | null
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

/**
 * Typed error thrown by ``ApiClient.request`` on a !ok HTTP response.
 *
 * Pre-PR (silent-error UX bug surfaced by Firefox-MCP click-through on
 * 2026-06-17 against v5.0.47): the request layer only threw
 * ``new Error('API Error: 400 Bad Request')`` — the status line, no
 * body. The server's carefully-worded validation message (e.g. PR
 * #163's "invalid agent_id 'BadName!@#': must match ...") was logged
 * to console but never reached the UI; mutation handlers'
 * ``console.error`` swallow-pattern then made the failure invisible.
 *
 * ``ApiError`` carries:
 *   - ``status``  HTTP status code (e.g. 400 / 404 / 500).
 *   - ``message`` Best-effort human-readable text — preferring the
 *                 server's JSON ``{message: ...}`` field, falling back
 *                 to ``{detail}`` / ``{error}`` / raw body / status
 *                 line so toasts never end up empty.
 *   - ``body``    Raw response text for callers / logs that want the
 *                 full payload (parsing failures still preserved).
 *
 * Callers in components/ pass the caught error to ``toastError`` from
 * ``components/ui/toast`` which prefers ``err.message`` so the user
 * sees what the server actually said.
 */
export class ApiError extends Error {
  readonly status: number
  readonly body: string

  constructor(status: number, message: string, body: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
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
    //
    // PR D (prancy-napping-pie): credentials='include' so the
    // ``agent_mcp_session`` cookie is sent with every fetch. The
    // cookie is set by /agent-mcp/login (PR C) and is what
    // authenticates dashboard mutations now that the body-token
    // path is retired. Same-origin requests still attach the cookie
    // with omit (Path matches), but credentials='include' covers
    // cross-origin dev setups too (the cookie's SameSite=Lax
    // attribute keeps it scoped sensibly).
    const fetchOptions: RequestInit = {
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/vnd.agent-mcp.v1+json',
        // Don't set Origin header - let browser handle it automatically
        ...options.headers,
      },
      credentials: 'include',
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
        // PR D (prancy-napping-pie): on a 401 from any mutation OR
        // read, the operator's session cookie has expired (or was
        // never set). Bounce to /agent-mcp/login and preserve the
        // current path in ?next= so we land back here post-login.
        //
        // Guard the redirect with the standard SSR check (typeof
        // window) so this method stays safe to call from Next.js
        // server components / tests that import the singleton.
        // Also guard against an infinite loop: if we're already on
        // the login page, skip the bounce.
        if (
          r.status === 401 &&
          typeof window !== 'undefined' &&
          !window.location.pathname.endsWith('/agent-mcp/login')
        ) {
          const next = window.location.pathname + window.location.search
          window.location.assign(loginUrl(next))
          // Throw so the caller's `.catch` doesn't accidentally
          // surface stale data; the navigation will tear down the
          // page before this matters in practice.
          throw new ApiError(401, 'session expired; redirecting to login', errorText)
        }
        // Only log non-404 errors
        if (r.status !== 404) {
          console.error(`API Error [${r.status}]:`, errorText)
        }
        // Prefer the server's JSON ``{message: ...}`` payload (the
        // 400 / 422 / 500 paths in agent_mcp/api/* all emit a
        // ``message`` field — see
        // tests/test_dashboard_create_agent_endpoint.py). Fall back
        // through ``detail`` (FastAPI default) / ``error`` (some
        // legacy endpoints) / raw body / status line so the surfaced
        // ``error.message`` is never an empty string.
        let surfaced = `${r.status} ${r.statusText}`.trim()
        try {
          const parsed = JSON.parse(errorText)
          if (parsed && typeof parsed === 'object') {
            const candidate =
              (typeof parsed.message === 'string' && parsed.message) ||
              (typeof parsed.detail === 'string' && parsed.detail) ||
              (typeof parsed.error === 'string' && parsed.error) ||
              ''
            if (candidate) {
              surfaced = candidate
            }
          }
        } catch {
          // Body wasn't JSON — fall back to the raw text if
          // non-empty, otherwise keep the status-line default.
          if (errorText && errorText !== 'Unknown error') {
            surfaced = errorText
          }
        }
        throw new ApiError(r.status, surfaced, errorText)
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
    // PR D (prancy-napping-pie): the operator session cookie carries
    // auth — no body-token field. Browsers attach the cookie
    // automatically. The 401-handling redirect in this.request
    // bounces the operator to /agent-mcp/login if the cookie has
    // expired between page-load and click.
    return this.request('/agents', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async terminateAgent(agentId: string): Promise<{ success: boolean; message: string }> {
    // PR D: cookie auth, no body-token field.
    return this.request('/terminate-agent', {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId }),
    })
  }

  // Restore + Purge for terminated agents. `restoreAgent` flips the
  // soft-delete back; `getPurgePreview` returns blast-radius counts
  // and samples for the confirmation modal; `purgeAgent` runs the
  // cascade tombstone + DELETE. PR D: cookie auth.
  async restoreAgent(
    agentId: string,
  ): Promise<{ success: boolean; agent_id: string; status: string; message: string }> {
    return this.request(`/agents/${encodeURIComponent(agentId)}/restore`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
  }

  // editAgent updates the editable agent fields (capabilities, color,
  // working_directory, aoe_session_id). PR D: cookie auth; backed by
  // POST /api/agents/<id>/edit. aoe_session_id is a 16-char lowercase
  // hex string (or empty to clear) used by the AoE notification side-
  // channel — see features/aoe_notify.py.
  async editAgent(
    agentId: string,
    updates: {
      capabilities?: string[]
      color?: string
      working_directory?: string
      aoe_session_id?: string
      // Event-coord PR-1: per-agent wake-loop toggle. Whitelisted on
      // the server side in /api/agents/<id>/edit.
      auto_event_loop?: boolean
    },
  ): Promise<{ success: boolean; agent_id: string; updated: Record<string, unknown>; message: string }> {
    return this.request(
      `/agents/${encodeURIComponent(agentId)}/edit`,
      {
        method: 'POST',
        body: JSON.stringify(updates),
      },
    )
  }

  // aoeHealth probes the configured Agents-of-Empires instance with
  // the current bearer token. Settings panel uses it to warn when the
  // token has gone stale (AoE rotates the file-sourced token on a
  // schedule). PR D: cookie auth (was ?token=<admin> query param).
  async aoeHealth(): Promise<{
    status: 'ok' | 'disabled' | 'unauthorized' | 'unreachable' | 'misconfigured'
    message?: string
    session_count?: number
    base_url?: string
  }> {
    return this.request(`/aoe/health`)
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
    // PR D: cookie auth (was ?token=<admin> query param).
    return this.request(
      `/agents/${encodeURIComponent(agentId)}/purge-preview`,
    )
  }

  async purgeAgent(agentId: string): Promise<{
    success: boolean
    agent_id: string
    tombstone: string
    counts: Record<string, number>
    message: string
  }> {
    // PR D: cookie auth.
    return this.request(
      `/agents/${encodeURIComponent(agentId)}?cascade=true`,
      {
        method: 'DELETE',
        body: JSON.stringify({}),
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
    // PR D (prancy-napping-pie): cookie auth, no body-token field.
    const body: Record<string, unknown> = { task_id: taskId }
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
    // Event-coord PR-1: free-text capability labels for the
    // routing-on-unassigned-task path shipped in PR-2. Server
    // normalizes (lowercase + strip + dedupe) at write time.
    required_capabilities?: string[]
  }): Promise<{ success: boolean; message: string; task_id?: string }> {
    // PR D (prancy-napping-pie): cookie auth, no body-token field.
    return this.request('/tasks', {
      method: 'POST',
      body: JSON.stringify({
        task_title: data.title,
        task_description: data.description ?? '',
        priority: data.priority,
        assigned_to: data.assigned_to,
        parent_task: data.parent_task,
        required_capabilities: data.required_capabilities,
      }),
    })
  }

  // Delete a task via DELETE /api/tasks/<id> (added upstream in dvaerum#12).
  // PR D: cookie auth.
  async deleteTask(taskId: string): Promise<{ success: boolean; message: string }> {
    return this.request(`/tasks/${taskId}`, {
      method: 'DELETE',
      body: JSON.stringify({}),
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