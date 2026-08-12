// System / composition resource module — status, relationship graphs,
// the bulk `/all-data` envelope, tokens, the prompt-book catalog, and
// node-details. These are the cross-cutting composition reads that
// don't belong to a single resource.

import type { ApiClient } from './client'
import { ShapeError, isRecord, describe } from './client'
import type { Agent } from './agents'
import { type RawTask, type Task, normalizeTask } from './tasks'
import type { RawContextEntry } from './memories'

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

export interface SystemStatus {
  server_running: boolean
  total_agents: number
  active_agents: number
  total_tasks: number
  pending_tasks: number
  completed_tasks: number
  last_updated: string
}

/** Runtime shape guard for the `/status` read boundary. */
export function systemStatusGuard(data: unknown): SystemStatus {
  if (!isRecord(data) || typeof data.server_running !== 'boolean') {
    throw new ShapeError(
      `GET /status: expected a SystemStatus object with a boolean ` +
        `server_running, got ${describe(data)}`,
    )
  }
  return data as unknown as SystemStatus
}

/**
 * The `/all-data` bulk envelope exactly as it arrives on the wire —
 * BEFORE the boundary normalizes the raw task rows (`tasks: RawTask[]`).
 */
export interface RawAllData {
  agents: Agent[]
  tasks: RawTask[]
  context: RawContextEntry[]
  actions: unknown[]
  file_metadata: unknown[]
  file_map: Record<string, unknown>
  timestamp: string
}

/**
 * Runtime shape guard for the `/all-data` read boundary (TY-1). This is
 * the highest-traffic read path — it feeds agents / tasks / context to
 * every page — yet it used to be a bare trusted cast. A structurally-
 * wrong 200 (agents/tasks/context missing or renamed) would then sail
 * through the seam and blow up far away in a consumer's `.find`/`.map`
 * (e.g. `selectAgent` via the prompt-book Run handler). Asserting the
 * three consumer-critical fields are arrays makes that fail loudly HERE,
 * naming the endpoint, rather than deep in the store.
 */
export function allDataGuard(data: unknown): RawAllData {
  if (!isRecord(data)) {
    throw new ShapeError(
      `GET /all-data: expected an envelope object, got ${describe(data)}`,
    )
  }
  for (const key of ['agents', 'tasks', 'context'] as const) {
    if (!Array.isArray(data[key])) {
      throw new ShapeError(
        `GET /all-data: expected \`${key}\` to be an array, got ` +
          `${describe(data[key])}`,
      )
    }
  }
  return data as unknown as RawAllData
}

/**
 * System/composition client methods bound to a shared request core.
 * Assembled onto the composed client by `createApiClient()`.
 */
export function systemApi(core: ApiClient) {
  return {
    // System endpoints
    getSystemStatus(): Promise<SystemStatus> {
      return core.request<SystemStatus>('/status', {}, systemStatusGuard)
    },

    getGraphData(): Promise<{ nodes: GraphNode[]; edges: GraphEdge[] }> {
      return core.request<{ nodes: GraphNode[]; edges: GraphEdge[] }>('/graph-data')
    },

    getTaskTreeData(): Promise<{ nodes: GraphNode[]; edges: GraphEdge[] }> {
      return core.request<{ nodes: GraphNode[]; edges: GraphEdge[] }>('/task-tree-data')
    },

    // Token endpoints
    //
    // Wave 2 (cleanup-wave-2): ``admin_token`` is intentionally NOT
    // declared on the return type even though the backend still ships
    // it in the JSON payload. Wave 3 will drop it from the response
    // entirely; until then, the type guarantees that no frontend
    // consumer can read it (TypeScript will reject every access).
    getTokens(): Promise<{
      agent_tokens: Array<{ agent_id: string; token: string }>
    }> {
      return core.request('/tokens')
    },

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
    getPromptsCatalog(): Promise<{
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
      return core.request('/prompts/catalog')
    },

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
      context: RawContextEntry[]
      actions: unknown[]
      file_metadata: unknown[]
      file_map: Record<string, unknown>
      timestamp: string
    }> {
      // The bulk envelope carries raw task rows (child_tasks /
      // depends_on_tasks as JSON strings); TY-2 normalize them to arrays
      // so getAllData() and getTasks() hand back identically-shaped Tasks.
      const env = await core.request<RawAllData>(
        '/all-data',
        {},
        allDataGuard,
      )
      return { ...env, tasks: (env.tasks ?? []).map(normalizeTask) }
    },

    // Node details endpoint
    getNodeDetails(nodeId: string): Promise<{
      id: string
      type: string
      data: Record<string, unknown>
      actions: Array<Record<string, unknown>>
      related?: Record<string, unknown>
    }> {
      return core.request(`/node-details?node_id=${encodeURIComponent(nodeId)}`)
    },
  }
}
