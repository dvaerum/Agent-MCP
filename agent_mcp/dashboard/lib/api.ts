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
// unions (status: 'pending' | 'running' | ...) the bare DB column
// types can't express. As callers migrate to the generated types,
// the manual declarations here will get trimmed.
export * from './api-types.generated'

import { apiUrl, loginUrl } from './urls'

export interface Agent {
  agent_id: string
  status: 'pending' | 'running' | 'terminated' | 'failed'
  current_task?: string
  working_directory?: string
  color?: string
  created_at: string
  updated_at: string
  terminated_at?: string | null
  auth_token?: string
  // 16-char lowercase hex string identifying the matching session
  // used by the ADR-0021 delivery bridge to route messages to this
  // agent. Empty/missing = no binding (delivery falls back to
  // title-match resolution).
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
  // Phase 2 Wave 2b (plan §2e): role tier. Defaults to 'worker' for
  // any legacy row that pre-dates Wave 1a's migration. 'manager'
  // grants the manager-tier privileges Wave 3 enforces on tool calls.
  agent_role?: 'worker' | 'manager'
  // Wave 7 PR 2 — coordinator transition. Live MCP-session presence
  // derived server-side from `agent_mcp/core/session_registry.py`:
  // - `online: true` iff this agent's bearer is currently subscribed
  //   to a live /mcp stream (runtime queue attached).
  // - `last_mcp_connection`: ISO-UTC `last_seen_at` of the most
  //   recent MCP session this agent opened in the current backend
  //   process (NULL when no stream has ever been opened — the
  //   "Pending — paste snippet into claude .mcp.json" case).
  // Replaces spawn-lifecycle badges in the agents list. Old clients
  // that don't read these fields stay on the legacy `status` field
  // (which is still surfaced, just not the source of the badge).
  online?: boolean
  last_mcp_connection?: string | null
  // Agent self-service profile (migration 0018): the agent's own
  // free-text "what I do / how I work / what to ask me" description,
  // authored via the update_agent_profile MCP tool and curatable by the
  // operator via POST /api/agents/<id>/edit. NULL/'' = never set.
  profile?: string | null
  profile_updated_at?: string | null
  profile_updated_by?: string | null
  profile_reviewed_at?: string | null
  // ADR-0021 delivery transport liveness, reported per-agent by the
  // delivery transport worker (`_mcp_presence_for` in
  // app/routers/agents.py). Distinct from `agentPresence()` (which
  // reflects the live /mcp stream): this is the delivery side-channel's
  // own view of whether the agent is actively working / idle / dormant /
  // dead. NULL or absent when no delivery transport has reported for
  // this agent yet — the UI renders nothing in that case.
  transport_status?: TransportStatus | null
}

/** ADR-0021 delivery transport per-agent liveness. Reported by the
 *  delivery transport worker; separate axis from MCP-stream presence
 *  (`AgentPresence`). */
export type TransportStatus = 'working' | 'idle' | 'dormant' | 'dead'

/** Wave 7 PR 2 — derived presence kind for the agents list / detail
 *  panel. Sourced from the new `online` + `last_mcp_connection`
 *  fields plus the existing `status` column (terminated takes
 *  precedence so a terminated agent never shows "Online" even
 *  during the brief window before its bearer's MCP stream tears
 *  down on the wire).
 *
 *  - `terminated`: row's status column is `terminated`.
 *  - `online`: live MCP stream attached.
 *  - `offline`: registered + previously connected, no live stream.
 *  - `pending`: registered + never connected (the operator hasn't
 *    pasted the snippet into the user's claude yet). */
export type AgentPresence = 'online' | 'offline' | 'pending' | 'terminated'

export function agentPresence(agent: Agent): AgentPresence {
  if (agent.status === 'terminated') return 'terminated'
  if (agent.online) return 'online'
  if (agent.last_mcp_connection) return 'offline'
  return 'pending'
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

// A message row returned by POST /api/messages/query.
// v5.0.22: subject (root-only) + parent_message_id (NULL for roots,
// reply→root.message_id for replies). Canonical home for the shape
// shared by messages-dashboard, its row/modal components, and the
// messages mobile list.
export interface Message {
  message_id: string
  sender_id: string
  recipient_id: string
  message_content: string
  message_type: string
  priority: string
  timestamp: string
  delivered: number | boolean
  read: number | boolean
  // Subject display value: a real (sender-chosen or model-generated)
  // subject, OR — when `subject_is_placeholder` is true — a computed
  // 50-char preview of the body standing in for a not-yet-set subject.
  subject: string | null
  // True when `subject` is an auto-generated preview, not a real subject
  // (the backend stores NULL until Phase 2's backfill titles it; the
  // read path returns a preview + this flag). Absent/false = real subject
  // or a reply (which carries no subject). See backend Phase 1/2.
  subject_is_placeholder?: boolean
  parent_message_id: string | null
}

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

// A project_settings row (ADR-0016: config_* keys live in the dedicated
// settings store, not project_context). `value` is the raw JSON-encoded
// string the store carries; secret keys arrive as the literal string
// "[redacted]" for non-confirmed tiers.
export interface ProjectSetting {
  context_key: string
  value: string
  description?: string | null
  created_at?: string | null
  created_by?: string | null
  updated_at: string
  updated_by: string
}

// A single setting's schema entry, as returned by
// GET /api/settings-schema (ADR-0018). The backend registry
// (agent_mcp/core/settings_schema.py) is the single source of truth
// for every setting's default / grouping / tier / copy; the Settings
// dashboard renders itself from this list via a type→widget registry
// rather than hardcoding the spec table.
export interface SettingsSchemaEntry {
  key: string
  type: 'bool' | 'int' | 'string' | 'secret'
  default: unknown
  tier: 'operator' | 'sysadmin'
  group: 'worker_permissions' | 'event_loop' | 'retention' | 'agent_profiles' | 'scheduling'
  title: string
  description: string
  widget:
    | 'switch'
    | 'int_days'
    | 'int_ms'
    | 'int_duration'
    | 'url'
    | 'secret'
    | 'secret_path'
    | 'template'
}

// GET /api/settings-schema envelope. `caller` reports the requesting
// operator's tier so the UI can render sysadmin-tier controls disabled
// for a plain operator (instead of letting the save 403).
export interface SettingsSchemaResponse {
  schema: SettingsSchemaEntry[]
  caller: {
    sysadmin: boolean
    confirmed_operator: boolean
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

    // Add timeout support.
    //
    // Cold-start abort fix (P005, 2026-06-19): the per-request timeout
    // must safely outlast the router's lazy-spawn socket-wait. The
    // orchestrator (`agent_mcp/router/project_orchestrator.py`) waits
    // up to 20 s for a freshly-started backend's Unix socket to appear
    // before surfacing 504 Gateway Timeout. A request that lands while
    // the backend is still spawning blocks inside the proxy until the
    // socket exists. If the client's `AbortController` fires first,
    // every in-flight per-project fetch on the dashboard's first paint
    // is cancelled with `NS_BINDING_ABORTED` — the main panel renders
    // empty because the 5xx-retry loop below never sees a status code,
    // only a discarded promise.
    //
    // 30 s covers the orchestrator's 20 s socket budget plus the
    // 200 ms + 400 ms retry backoff and HTTP round-trip overhead, so
    // the first cold request either returns a real response (success
    // or 504) or — far more likely — a 5xx the retry loop catches and
    // retries against an already-warm backend.
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 30000) // 30 second timeout

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
          // ADR-0020: compare against the mount-derived login path
          // (loginUrl() = `${ROOT}/login`) so the loop-guard holds at
          // both the tailnet (/agent-mcp/login) and root (/login) mounts.
          !window.location.pathname.endsWith(loginUrl())
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
    // Wave 2 (cleanup-wave-2): the Admin pseudo-agent's auth_token used
    // to fall back to ``tokens.admin_token``; Wave 3 will drop that
    // field from the /api/tokens response entirely and Wave 4 deletes
    // the Admin pseudo-agent. The dashboard runs as admin via cookie
    // auth now (ADR-0003), so no admin-side bearer is needed in the UI.
    const tokens = await this.getTokens()
    const agentToken = tokens.agent_tokens.find(t => t.agent_id === agentId)?.token
    
    // Get node details which includes actions and related tasks
    const nodeDetails = await this.getNodeDetails(`agent_${agentId}`)
    
    return {
      agent: { ...agent, auth_token: agentToken },
      token: agentToken,
      tasks: (nodeDetails.related?.assigned_tasks as Task[]) || [],
      actions: (nodeDetails.actions as any[]) || []
    }
  }

  // Wave 7 PR 3 (coordinator transition): ``createAgent`` (POST
  // /api/agents — the spawn-via-tmux path) is gone. ``registerAgent``
  // below is the sole agent-creation surface.

  async terminateAgent(agentId: string): Promise<{ success: boolean; message: string }> {
    // PR D: cookie auth, no body-token field.
    return this.request('/terminate-agent', {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId }),
    })
  }

  // Wave 7 coordinator transition. Register an agent identity
  // WITHOUT spawning a claude process: the backend mints a token + a
  // ready-to-paste .mcp.json snippet that the operator hands to the
  // user. The user owns their own claude session.
  async registerAgent(data: {
    name: string
    role?: 'worker' | 'manager'
    // The frontend supplies these so the backend's snippet builder
    // doesn't have to derive them from request headers (which the
    // per-project backend gets after the router proxy strips Host).
    project_name?: string | null
    host?: string
    // ADR-0020: the client's external mount prefix (deriveMount()) —
    // "" at a root front door, "/agent-mcp" on the tailnet — so the
    // returned .mcp.json snippet URL matches the front door in use.
    mount_prefix?: string
  }): Promise<{
    success?: boolean
    message: string
    agent_id?: string
    agent_token?: string
    agent_role?: string
    mcp_snippet?: string
    project_name?: string | null
  }> {
    return this.request('/agents/register', {
      method: 'POST',
      body: JSON.stringify(data),
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

  // Disconnect / Reconnect — pause or resume an agent's monitoring loop
  // WITHOUT terminating it or revoking its token. Disconnect sets
  // auto_event_loop OFF (its wait_for_events starts returning
  // stop_listening with an operator-facing reason), wakes the parked
  // long-poll to deliver that now, and closes the live push stream so the
  // agent flips offline. Reconnect flips it back ON. The fleet variants
  // toggle the GLOBAL loop — "we're done for now" / "we're back".
  async disconnectAgent(
    agentId: string,
  ): Promise<{ success: boolean; agent_id: string; closed_streams: number; message: string }> {
    return this.request(`/agents/${encodeURIComponent(agentId)}/disconnect`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
  }

  async reconnectAgent(
    agentId: string,
  ): Promise<{ success: boolean; agent_id: string; message: string }> {
    return this.request(`/agents/${encodeURIComponent(agentId)}/reconnect`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
  }

  async disconnectAllAgents(): Promise<{ success: boolean; closed_streams: number; message: string }> {
    return this.request('/agents/disconnect-all', {
      method: 'POST',
      body: JSON.stringify({}),
    })
  }

  async reconnectAllAgents(): Promise<{ success: boolean; message: string }> {
    return this.request('/agents/reconnect-all', {
      method: 'POST',
      body: JSON.stringify({}),
    })
  }

  // editAgent updates the editable agent fields (color,
  // working_directory, aoe_session_id). PR D: cookie auth; backed by
  // POST /api/agents/<id>/edit. aoe_session_id is a 16-char lowercase
  // hex string (or empty to clear) used by the ADR-0021 delivery
  // bridge to route messages to this agent.
  async editAgent(
    agentId: string,
    updates: {
      color?: string
      working_directory?: string
      aoe_session_id?: string
      // Event-coord PR-1: per-agent wake-loop toggle. Whitelisted on
      // the server side in /api/agents/<id>/edit.
      auto_event_loop?: boolean
      // Phase 2 Wave 2b (plan §2e): promote a worker to manager (or
      // demote). Whitelisted on the server side; the API-boundary
      // check 422s anything outside {'worker', 'manager'}.
      agent_role?: 'worker' | 'manager'
      // Agent self-description curation. Routed server-side through
      // review_profile so the updated_at/by/reviewed_at bookkeeping
      // matches the agent's own MCP self-edit. Empty string clears it.
      profile?: string
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
  //
  // Optional server-side filters (status / assignment / creator) drive
  // GET /tasks — the single source of truth shared with the backend +
  // the MCP `view_tasks` tool. Called with no args it stays a plain
  // `GET /tasks` (back-compat). See `buildTasksQuery` / `TaskFilters`.
  async getTasks(filters?: TaskFilters): Promise<Task[]> {
    return this.request<Task[]>(`/tasks${buildTasksQuery(filters)}`)
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
      }),
    })
  }

  // Blast radius of a task delete — the descendant subtree plus the two
  // other conditions the backend refuses on (dependents / an agent's
  // current_task). Mirrors `getPurgePreview`. The delete dialog reads
  // `requires_force` to choose its confirmation tier.
  async getTaskDeletePreview(taskId: string): Promise<{
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
    return this.request(
      `/tasks/${encodeURIComponent(taskId)}/delete-preview`,
    )
  }

  // Delete a task via DELETE /api/tasks/<id> (added upstream in dvaerum#12).
  // PR D: cookie auth.
  //
  // `force` is the operator's EXPLICIT cascade confirmation and defaults
  // to false. The route used to hardcode `force_delete=true` server-side,
  // which made the backend's cascade guard dead code on this surface: one
  // click deleted a whole descendant subtree. Only the tier-2 branch of
  // the delete dialog — the one that showed the count and made the
  // operator type DELETE — may pass true.
  async deleteTask(
    taskId: string,
    opts?: { force?: boolean },
  ): Promise<{ success: boolean; message: string }> {
    return this.request(`/tasks/${taskId}`, {
      method: 'DELETE',
      body: JSON.stringify({ force_delete: opts?.force === true }),
    })
  }

  // Token endpoints
  //
  // Wave 2 (cleanup-wave-2): ``admin_token`` is intentionally NOT
  // declared on the return type even though the backend still ships
  // it in the JSON payload. Wave 3 will drop it from the response
  // entirely; until then, the type guarantees that no frontend
  // consumer can read it (TypeScript will reject every access).
  async getTokens(): Promise<{
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
  //
  // Wave 2 (cleanup-wave-2): ``admin_token`` is intentionally NOT
  // declared on the return type even though the backend still ships
  // it in the JSON payload. Wave 3 will drop it from the response
  // entirely; until then, the type guarantees that no frontend
  // consumer can read it (TypeScript will reject every access).
  async getAllData(): Promise<{
    agents: Agent[]
    tasks: Task[]
    context: any[]
    actions: any[]
    file_metadata: any[]
    file_map: Record<string, any>
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

  // Memory endpoints. Wave 2 (cleanup-wave-2): the dashboard no
  // longer threads an admin bearer into the request body — auth is
  // carried by the operator session cookie that ``request()`` sends
  // with ``credentials: 'include'``. The backend's
  // ``require_operator_session`` dep admits cookie / bearer / body-
  // token interchangeably, so omitting the body token is a no-op
  // for non-cookie callers (legacy admin scripts).
  async createMemory(data: {
    context_key: string
    context_value: any
    description?: string
  }): Promise<{ success: boolean; message: string }> {
    return this.request('/memories', {
      method: 'POST',
      body: JSON.stringify(data)
    })
  }

  async updateMemory(context_key: string, data: {
    context_value: any
    description?: string
  }): Promise<{ success: boolean; message: string }> {
    return this.request(`/memories/${encodeURIComponent(context_key)}`, {
      method: 'PUT',
      body: JSON.stringify(data)
    })
  }

  async deleteMemory(context_key: string): Promise<{ success: boolean; message: string }> {
    return this.request(`/memories/${encodeURIComponent(context_key)}`, {
      method: 'DELETE',
      body: JSON.stringify({})
    })
  }

  // Project settings endpoints (ADR-0016). The Settings tab's store —
  // config_* toggles/knobs live in project_settings, written via the
  // gated update/delete_project_settings tools (system.config.write
  // cap; config settings are uniformly operator-tier). Same
  // cookie-session auth story as the memory endpoints above.
  async getSettingsData(): Promise<{ settings: ProjectSetting[] }> {
    return this.request('/settings-data')
  }

  // Settings schema (ADR-0018). The backend registry owns every
  // setting's default / grouping / tier / copy; the Settings dashboard
  // renders itself from this list. `caller.sysadmin` drives tier-aware
  // rendering (sysadmin-tier controls disabled for a plain operator).
  // Reads only — writes still go through create/update/deleteSetting.
  async getSettingsSchema(): Promise<SettingsSchemaResponse> {
    return this.request('/settings-schema')
  }

  // Scheduled directives (event-loop scheduler). Operator/admin surface
  // behind the Schedules tab; all routes are require_operator_session-
  // gated. The backend reuses the same guardrail-enforcing tool impls the
  // agent surface uses, so the floor/max checks apply here too.
  async getSchedules(): Promise<Schedule[]> {
    const res = await this.request<{ schedules: Schedule[] }>('/schedules')
    return res.schedules ?? []
  }

  async createSchedule(data: {
    agent_id: string
    prompt: string
    interval_seconds: number
    until?: string | null
    count?: number | null
    run_now?: boolean
  }): Promise<{ success: boolean; directive: Schedule }> {
    return this.request('/schedules', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async updateSchedule(directiveId: string, data: {
    prompt?: string
    interval_seconds?: number
    enabled?: boolean
    until?: string | null
    count?: number | null
  }): Promise<{ success: boolean; directive: Schedule }> {
    return this.request(`/schedules/${encodeURIComponent(directiveId)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    })
  }

  async deleteSchedule(directiveId: string): Promise<{ success: boolean; deleted: string }> {
    return this.request(`/schedules/${encodeURIComponent(directiveId)}`, {
      method: 'DELETE',
      body: JSON.stringify({}),
    })
  }

  // Ad-hoc poke (operator/admin only) — push a one-shot directive to an
  // agent. Delivered immediately if it's listening, else queued urgent.
  async pokeAgent(agentId: string, data: {
    prompt: string
    priority?: string
  }): Promise<{
    success: boolean
    poke_id: string
    agent_id: string
    // TRUE when the agent had a parked wait_for_events waiter and the
    // poke was delivered immediately; FALSE when it was queued as its
    // highest-priority next check-in. Drives the delivered-vs-queued
    // toast copy (see SendDirectiveModal).
    delivered: boolean
    message: string
  }> {
    return this.request(`/agents/${encodeURIComponent(agentId)}/directive`, {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async createSetting(data: {
    context_key: string
    context_value: any
    description?: string
  }): Promise<{ success: boolean; message: string }> {
    return this.request('/settings', {
      method: 'POST',
      body: JSON.stringify(data)
    })
  }

  async updateSetting(context_key: string, data: {
    context_value: any
    description?: string
  }): Promise<{ success: boolean; message: string }> {
    return this.request(`/settings/${encodeURIComponent(context_key)}`, {
      method: 'PUT',
      body: JSON.stringify(data)
    })
  }

  async deleteSetting(context_key: string): Promise<{ success: boolean; message: string }> {
    return this.request(`/settings/${encodeURIComponent(context_key)}`, {
      method: 'DELETE',
      body: JSON.stringify({})
    })
  }

  async getMemoryHealth(): Promise<MemoryHealthAnalysis> {
    return this.request('/memories/health', {
      method: 'POST',
      body: JSON.stringify({ show_health_analysis: true })
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
}

// Create singleton instance
export const apiClient = new ApiClient()

// Feature 1 (message-threads-ui): fetch the WHOLE conversation a message
// belongs to — the root plus every reply transitively descending from it,
// ordered oldest-first (root first). Backs the conversation view in
// <ViewMessageModal>. Hits GET /api/{project}/messages/{id}/thread through
// the same cookie-auth + strict-v1-media-type wrapper the other message
// calls use; returns data.thread. When ``projectName`` is empty (the
// standalone single-tenant deploy) it falls back to the ApiClient's
// configured API root so both deployment shapes resolve correctly.
export async function getMessageThread(
  projectName: string,
  messageId: string,
): Promise<Message[]> {
  const rest = `messages/${encodeURIComponent(messageId)}/thread`
  const url = projectName
    ? apiUrl(projectName, rest)
    : `${apiClient.getServerUrl()}/${rest}`
  const res = await fetch(url, {
    method: 'GET',
    headers: {
      // PR-A: REST endpoints require the strict v1 media type.
      Accept: 'application/vnd.agent-mcp.v1+json',
    },
    credentials: 'include',
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(txt || `HTTP ${res.status}`)
  }
  const data = await res.json()
  return Array.isArray(data?.thread) ? (data.thread as Message[]) : []
}
