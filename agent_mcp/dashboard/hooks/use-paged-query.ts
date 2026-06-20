"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"

/**
 * Generic paginated-fetch state machine — the lone owner of the
 * "data + total + loading + error + refresh + lastFetch" sextuplet
 * that the dashboard's two-and-a-half paginated query call-sites used
 * to hand-roll three different ways.
 *
 * PR 5 of the 2026-06-09 architecture review series. The migration
 * targets:
 *
 *   * ``messages-dashboard.tsx`` — calls ``POST /api/messages/query``
 *     via a bespoke ``callMessages`` helper, builds the body inline
 *     (``{limit, offset, ...filters}``), threads four useState
 *     vars (``messages``, ``loading``, ``error``, ``total``), wires a
 *     refresh effect on ``[filters, currentOffset]``. The hook
 *     subsumes this 1:1 — its default POST path IS the
 *     ``/api/messages/query`` shape. Wave 2 (cleanup-wave-2)
 *     dropped the admin-token body field; auth is the operator
 *     session cookie sent with ``credentials: "include"``.
 *
 *   * ``tasks-dashboard.tsx`` — wraps ``apiClient.getTasks()`` (GET
 *     ``/tasks``, no body, no pagination, no filter spread) in a
 *     private ``useTasksData`` hook that owned the same 4-tuple plus
 *     a 30s cache and a 60s background refresh interval. The hook
 *     drives this case via the ``fetchFn`` escape hatch — the state
 *     machine is shared, only the underlying request differs.
 *
 *   * ``agents-dashboard.tsx`` — does NOT have its own fetch. Reads
 *     agents out of the global ``useDataStore`` (zustand) which
 *     fetches ALL dashboard data in one ``/api/all-data`` round-trip
 *     and multiplexes the result across every tab. This is a
 *     fundamentally different shape from a per-tab paginated query —
 *     there's no per-tab fetch state to consolidate, and forcing
 *     ``useDataStore`` into a ``fetchFn`` wrapper would just rename
 *     state without consolidating it. ``agents-dashboard.tsx`` is
 *     therefore intentionally out of scope for this PR.
 *
 * Default usage (messages-dashboard, the canonical cookie-auth shape):
 *
 *     const { data, total, loading, error, refresh, lastFetch } =
 *       usePagedQuery<Message>({
 *         endpoint: "/messages/query",  // appended to apiClient.getServerUrl()
 *         filters,                       // useFilters() snapshot
 *         limit: PAGE_SIZE,
 *         offset: currentOffset,
 *       })
 *
 * Escape-hatch usage (tasks-dashboard, GET /tasks via apiClient):
 *
 *     const { data: tasks, loading, error, refresh, lastFetch } =
 *       usePagedQuery<Task>({
 *         fetchFn: async (signal) => {
 *           const tasks = await apiClient.getTasks()
 *           return { data: tasks, total: tasks.length }
 *         },
 *         cacheMs: 30_000,
 *         deps: [activeServerId],
 *       })
 *
 * Design notes:
 *
 *   * ``data`` defaults to ``[]`` (never ``null``) — call sites can
 *     ``.map()`` over it on the very first render without a guard.
 *     ``total`` defaults to ``0`` for the same reason.
 *
 *   * ``error`` is a real ``Error`` instance, not a stringified
 *     message. Consumers that want the legacy ``string | null`` API
 *     wrap with ``error?.message ?? null``.
 *
 *   * In-flight fetches are cancelled via ``AbortController`` whenever
 *     the inputs change. A slow stale request must not be allowed to
 *     overwrite a fresh fast one (the classic race when an admin
 *     scrubs through filters faster than the network can answer).
 *     The previous ``callMessages`` helper had no cancellation — this
 *     is a real upgrade, not just a refactor.
 *
 *   * ``cacheMs`` is a TTL on the LAST successful response. Default
 *     0 = no caching (messages-dashboard). Tasks-dashboard's
 *     pre-migration ``useTasksData`` ran a 30s cache; the option
 *     preserves that behaviour without re-introducing a module-level
 *     ``Map<key, {data, timestamp}>``.
 *
 *   * The generic constraint is the open-ended ``object`` — same
 *     rationale as ``useFilters<T>`` (PR 4): ``Record<string,
 *     unknown>`` would reject interfaces, the call-sites would all
 *     have to rewrite their row type as a ``type`` alias with
 *     explicit index signature. ``object`` accepts both shapes; the
 *     per-row identity flows through the ``T[]`` return.
 *
 *   * ``fetchFn`` is the escape hatch for non-POST endpoints. It
 *     receives the ``AbortSignal`` and must return
 *     ``{data: T[], total: number}``. When ``fetchFn`` is supplied,
 *     ``endpoint`` / ``filters`` / ``limit`` / ``offset`` are ignored
 *     — ``fetchFn`` owns the request entirely. This is how
 *     tasks-dashboard's ``apiClient.getTasks()`` (which returns a
 *     bare ``Task[]`` with no envelope) plugs in.
 */
export interface UsePagedQueryOptions<T extends object> {
  /**
   * Endpoint path appended to ``apiClient.getServerUrl()``. The hook
   * POSTs ``{limit, offset, ...filters}`` as JSON with
   * ``credentials: "include"`` (auth = operator session cookie).
   * Ignored when ``fetchFn`` is supplied.
   */
  endpoint?: string
  /**
   * Escape hatch for non-POST endpoints. Receives the AbortSignal
   * so consumers can thread it into their own ``fetch`` call. Must
   * resolve to ``{data: T[], total: number}``. When present,
   * ``endpoint`` / ``filters`` / ``limit`` / ``offset`` are ignored.
   */
  fetchFn?: (signal: AbortSignal) => Promise<{ data: T[]; total: number }>
  /**
   * Filter snapshot — spread into the POST body. Use with
   * ``useFilters<F>().filters`` for the natural pairing.
   */
  filters?: object
  /** Page size. Defaults to undefined (server picks its default). */
  limit?: number
  /** Page offset. Defaults to 0. */
  offset?: number
  /**
   * Cache TTL on the last successful response, in ms. 0 = no caching
   * (default). Tasks-dashboard's pre-migration ``useTasksData`` ran
   * a 30s cache; pass 30_000 to preserve that behaviour.
   */
  cacheMs?: number
  /**
   * Extra dependencies for the refresh effect. Most consumers omit
   * (the hook already re-fetches when ``endpoint`` / ``filters`` /
   * ``limit`` / ``offset`` change). Tasks-dashboard passes
   * ``[activeServerId]`` so a server switch re-runs the fetch.
   */
  deps?: ReadonlyArray<unknown>
}

export interface UsePagedQueryResult<T extends object> {
  /** Rows. Defaults to []; never null. */
  readonly data: T[]
  /** Server-reported total for the active filter set. Defaults to 0. */
  readonly total: number
  /** True while a fetch is in flight. */
  readonly loading: boolean
  /** The last error encountered, or null. Real Error instance. */
  readonly error: Error | null
  /**
   * Re-run the fetch (bypasses any active cache TTL). Stable
   * identity — safe to pass to memoized children.
   */
  refresh: () => void
  /** Timestamp of the last successful fetch, or null. */
  readonly lastFetch: number | null
}

/**
 * Build the POST body matching the ``/api/messages/query`` contract.
 * Filters are spread last so they can override the framework keys
 * if a future endpoint redefines one (unlikely, but cheap to allow).
 *
 * Wave 2 (cleanup-wave-2): no ``token`` field — auth is carried by
 * the operator session cookie, sent via ``credentials: "include"``
 * on the fetch below.
 */
function buildPostBody(opts: {
  limit?: number
  offset?: number
  filters?: object
}): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  if (typeof opts.limit === "number") body.limit = opts.limit
  if (typeof opts.offset === "number") body.offset = opts.offset
  if (opts.filters) Object.assign(body, opts.filters)
  return body
}

export function usePagedQuery<T extends object>(
  options: UsePagedQueryOptions<T>,
): UsePagedQueryResult<T> {
  const {
    endpoint,
    fetchFn,
    filters,
    limit,
    offset,
    cacheMs = 0,
    deps,
  } = options

  // Initial-state literal is the bare empty array — typed via the
  // useState generic so consumers' ``.map()`` calls type-check on
  // the first render before any data lands.
  const [data, setData] = useState<T[]>([])
  const [total, setTotal] = useState<number>(0)
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<Error | null>(null)
  const [lastFetch, setLastFetch] = useState<number | null>(null)

  // Refs:
  //   * ``abortRef`` holds the AbortController for the in-flight
  //     fetch so a fresh request can cancel it before issuing its
  //     own (slow-stale-overwrites-fresh-fast race guard).
  //   * ``optionsRef`` is the closure target for ``refresh()`` —
  //     keeping the manual-refresh callback stable across renders
  //     without reaching into every input prop's identity.
  const abortRef = useRef<AbortController | null>(null)
  const optionsRef = useRef(options)
  optionsRef.current = options

  // Stable serialization of filters for the dependency array — the
  // call-site passes a fresh object literal on every render, so
  // we'd thrash the effect if we listed ``filters`` directly. The
  // shape is primitive-only across all consumers (same constraint
  // ``useFilters<T>``'s ``isActive`` relies on), so JSON.stringify is
  // safe.
  const filtersKey = useMemo(
    () => (filters ? JSON.stringify(filters) : ""),
    [filters],
  )

  // The core fetcher. Async + manual state writes — we don't use
  // a Promise.race here because AbortController gives us a cleaner
  // cancellation story (the in-flight request actually stops on the
  // network layer, not just "we ignore its result").
  const runFetch = useCallback(
    async (force: boolean): Promise<void> => {
      // Cache hit short-circuit (only when not forced and cacheMs > 0).
      const opts = optionsRef.current
      const ttl = opts.cacheMs ?? 0
      if (!force && ttl > 0 && lastFetch !== null) {
        const age = Date.now() - lastFetch
        if (age < ttl) return
      }

      // Cancel any prior in-flight fetch — the inputs have changed (or
      // the consumer hit Refresh) and we don't want a slow stale
      // response to overwrite the next one.
      if (abortRef.current) {
        abortRef.current.abort()
      }
      const controller = new AbortController()
      abortRef.current = controller
      const { signal } = controller

      setLoading(true)
      setError(null)
      try {
        let result: { data: T[]; total: number }
        if (opts.fetchFn) {
          result = await opts.fetchFn(signal)
        } else {
          if (!opts.endpoint) {
            throw new Error(
              "usePagedQuery: either `endpoint` or `fetchFn` must be supplied",
            )
          }
          const body = buildPostBody({
            limit: opts.limit,
            offset: opts.offset,
            filters: opts.filters,
          })
          // The dashboard's REST surface lives under the apiClient's
          // server URL. We import lazily so the hook stays standalone
          // (no top-level require — useful when tests stub the API
          // client). The endpoint string is appended verbatim.
          //
          // Wave 2 (cleanup-wave-2): ``credentials: "include"`` opts
          // into sending the ``agent_mcp_session`` cookie that the
          // backend's ``require_operator_session`` dep validates.
          const { apiClient } = await import("@/lib/api")
          const base = apiClient.getServerUrl()
          const res = await fetch(`${base}${opts.endpoint}`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              // PR-A: strict v1 media type on every REST endpoint.
              "Accept": "application/vnd.agent-mcp.v1+json",
            },
            body: JSON.stringify(body),
            credentials: "include",
            signal,
          })
          if (!res.ok) {
            const txt = await res.text().catch(() => "")
            throw new Error(txt || `HTTP ${res.status}`)
          }
          const json = await res.json()
          // The /query envelope shape: ``{messages|tasks|agents|...,
          // total, limit, offset}``. We pick the first array-valued
          // property as the rows. This keeps the hook agnostic to the
          // top-level key name without forcing a config knob — the
          // server already commits to "the rows live under exactly
          // one array key in the envelope".
          let rows: T[] = []
          let totalCount = 0
          if (json && typeof json === "object") {
            if (typeof json.total === "number") totalCount = json.total
            for (const key of Object.keys(json)) {
              if (key === "total" || key === "limit" || key === "offset") continue
              const val = (json as Record<string, unknown>)[key]
              if (Array.isArray(val)) {
                rows = val as T[]
                break
              }
            }
          }
          result = { data: rows, total: totalCount }
        }
        // If we got aborted mid-await (the controller fired between
        // ``await fetch`` and here), bail without writing state. The
        // next request owns the slot.
        if (signal.aborted) return
        setData(result.data)
        setTotal(result.total)
        setLastFetch(Date.now())
      } catch (e: unknown) {
        // AbortError surfaces as a DOMException with name === 'AbortError'
        // in browsers, or a regular Error in some polyfills. Either
        // way, an aborted fetch is a no-op for state.
        if (signal.aborted) return
        if (e instanceof Error && e.name === "AbortError") return
        const err = e instanceof Error ? e : new Error(String(e))
        setError(err)
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null
        }
        // Loading flips off regardless — even on abort, the request
        // is no longer in flight. A second runFetch will set it to
        // true again immediately.
        if (!signal.aborted) setLoading(false)
      }
    },
    // ``lastFetch`` is intentionally in the dep list so cacheMs gates
    // reactively when the timer rolls over. ``optionsRef`` covers the
    // rest of the inputs without bloating the deps.
    [lastFetch],
  )

  // Stable manual refresh. ``force=true`` bypasses cacheMs.
  const refresh = useCallback(() => {
    void runFetch(true)
  }, [runFetch])

  // The reactive effect: re-fetch when any input that affects the
  // request changes. ``filtersKey`` collapses object identity; the
  // rest are scalar. ``deps`` is appended verbatim (e.g.
  // ``[activeServerId]``).
  useEffect(() => {
    void runFetch(false)
    return () => {
      // Effect cleanup: abort any in-flight fetch the next render
      // will replace anyway. Saves bandwidth and stops the response
      // from landing on a stale closure.
      if (abortRef.current) {
        abortRef.current.abort()
        abortRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpoint, filtersKey, limit, offset, ...(deps ?? [])])

  return {
    data,
    total,
    loading,
    error,
    refresh,
    lastFetch,
  }
}
