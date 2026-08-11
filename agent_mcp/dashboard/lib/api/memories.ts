// Memories resource module — types, the shared context-row→Memory
// mapper, and the memory-scoped client methods.
//
// A "memory" is a project-context row. The dashboard reads them from
// the shared `/all-data` envelope (see `lib/queries/all-data.ts`) as
// raw `RawContextEntry` rows and shapes them into the richer `Memory`
// shape (size/staleness metadata) for display. That shaping happens in
// exactly ONE place now — `contextEntryToMemory` below — consumed by
// both `getMemories()` here and the memories dashboard's live view, so
// the two can't drift.

import type { ApiClient } from './client'

// Raw context row as it arrives in the /all-data + /context-data
// envelopes (pre-Memory-shaping). `contextEntryToMemory` maps these
// into the richer Memory shape.
export interface RawContextEntry {
  context_key: string
  value: unknown
  description?: string
  updated_at: string
  updated_by: string
  created_at?: string | null
  created_by?: string | null
}

export interface Memory {
  context_key: string
  value: unknown
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

/** A row older than this (ms) is flagged `is_stale`. */
const STALE_AGE_MS = 30 * 24 * 60 * 60 * 1000
/** A serialized value larger than this (bytes) is flagged `is_large`. */
const LARGE_SIZE_BYTES = 10240

/**
 * Shape a raw project-context row into the display `Memory` shape,
 * computing the size / staleness metadata the dashboard renders.
 *
 * This is the single source of truth for that mapping — both
 * `getMemories()` and the memories dashboard's live `/all-data` view
 * call it, so the two can't drift.
 *
 * W6-followup F1 bug fix: the previous inline mapping called
 * `JSON.stringify(ctx.value).length` — but `JSON.stringify(undefined)`
 * returns `undefined` (not a string), so a context row whose value was
 * `undefined` (or a non-serializable value) crashed the whole map with
 * `Cannot read properties of undefined (reading 'length')`. It also
 * re-serialized the value up to four times per row. Here the value is
 * serialized ONCE, undefined-safe (a missing value counts as 0 bytes).
 */
export function contextEntryToMemory(ctx: RawContextEntry): Memory {
  // JSON.stringify returns `undefined` for `undefined` / functions /
  // symbols; coalesce to '' so `.length` is always safe.
  const serialized = JSON.stringify(ctx.value) ?? ''
  const sizeBytes = serialized.length
  const updatedMs = ctx.updated_at ? new Date(ctx.updated_at).getTime() : NaN
  const hasUpdated = !Number.isNaN(updatedMs)
  return {
    context_key: ctx.context_key,
    value: ctx.value,
    description: ctx.description,
    updated_at: ctx.updated_at,
    updated_by: ctx.updated_by,
    created_at: ctx.created_at,
    created_by: ctx.created_by,
    _metadata: {
      size_bytes: sizeBytes,
      size_kb: Math.round((sizeBytes / 1024) * 100) / 100,
      // Values arrive already-parsed from the /all-data JSON envelope,
      // so a delivered value is by construction valid JSON.
      json_valid: true,
      days_old: hasUpdated
        ? Math.floor((Date.now() - updatedMs) / (1000 * 60 * 60 * 24))
        : undefined,
      is_stale: hasUpdated ? Date.now() - updatedMs > STALE_AGE_MS : false,
      is_large: sizeBytes > LARGE_SIZE_BYTES,
    },
  }
}

/** Options accepted by `getMemories`. */
export interface GetMemoriesOptions {
  /** Return only the row with this exact key. */
  context_key?: string
  /** Case-insensitive substring match over key / description / value. */
  search_query?: string
  /** Sort order (default: newest-updated first). */
  sort_by?: 'key' | 'updated_at' | 'size'
  /** Cap the number of rows returned. */
  max_results?: number
}

function sortMemories(
  memories: Memory[],
  sortBy: NonNullable<GetMemoriesOptions['sort_by']>,
): Memory[] {
  const sorted = [...memories]
  sorted.sort((a, b) => {
    switch (sortBy) {
      case 'key':
        return a.context_key.localeCompare(b.context_key)
      case 'size':
        return (b._metadata?.size_bytes ?? 0) - (a._metadata?.size_bytes ?? 0)
      case 'updated_at':
      default:
        return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
    }
  })
  return sorted
}

/**
 * Memory-scoped client methods bound to a shared request core.
 * Assembled onto the composed client by `createApiClient()`.
 */
export function memoriesApi(core: ApiClient) {
  return {
    /**
     * Read the project's memory (context) rows, shaped into `Memory`
     * rows with size/staleness metadata, honoring the caller's filter /
     * sort / limit options.
     *
     * W6-followup F1: the previous implementation accepted this options
     * object but IGNORED it entirely — every call returned the full
     * unfiltered, unsorted list — and crashed on a row whose value was
     * `undefined`. Both are fixed here: the mapping goes through the
     * undefined-safe `contextEntryToMemory`, and `context_key` /
     * `search_query` / `sort_by` / `max_results` are all applied.
     */
    async getMemories(options?: GetMemoriesOptions): Promise<Memory[]> {
      const env = await core.request<{ context: RawContextEntry[] }>('/all-data')
      let memories = (env.context ?? []).map(contextEntryToMemory)

      if (options?.context_key) {
        memories = memories.filter((m) => m.context_key === options.context_key)
      }
      if (options?.search_query) {
        const q = options.search_query.toLowerCase()
        memories = memories.filter(
          (m) =>
            m.context_key.toLowerCase().includes(q) ||
            (m.description?.toLowerCase().includes(q) ?? false) ||
            JSON.stringify(m.value ?? '').toLowerCase().includes(q),
        )
      }
      if (options?.sort_by) {
        memories = sortMemories(memories, options.sort_by)
      }
      if (options?.max_results != null && options.max_results >= 0) {
        memories = memories.slice(0, options.max_results)
      }
      return memories
    },

    // Memory write endpoints. Wave 2 (cleanup-wave-2): the dashboard no
    // longer threads an admin bearer into the request body — auth is
    // carried by the operator session cookie that ``request()`` sends
    // with ``credentials: 'include'``. The backend's
    // ``require_operator_session`` dep admits cookie / bearer / body-
    // token interchangeably, so omitting the body token is a no-op
    // for non-cookie callers (legacy admin scripts).
    createMemory(data: {
      context_key: string
      context_value: unknown
      description?: string
    }): Promise<{ success: boolean; message: string }> {
      return core.request('/memories', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },

    updateMemory(context_key: string, data: {
      context_value: unknown
      description?: string
    }): Promise<{ success: boolean; message: string }> {
      return core.request(`/memories/${encodeURIComponent(context_key)}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      })
    },

    deleteMemory(context_key: string): Promise<{ success: boolean; message: string }> {
      return core.request(`/memories/${encodeURIComponent(context_key)}`, {
        method: 'DELETE',
        body: JSON.stringify({}),
      })
    },

    getMemoryHealth(): Promise<MemoryHealthAnalysis> {
      return core.request('/memories/health', {
        method: 'POST',
        body: JSON.stringify({ show_health_analysis: true }),
      })
    },

    // Fetch the project context store (a.k.a. "memories"). Pairs with
    // getAllData() above, which already covers it but may 404 on
    // backends that don't implement the bulk endpoint.
    async getContextData(): Promise<unknown[]> {
      return core.request<unknown[]>('/context-data').catch(() => [])
    },
  }
}
