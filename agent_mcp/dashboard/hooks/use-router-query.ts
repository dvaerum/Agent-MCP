"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { ApiError } from "@/lib/api"

/**
 * Router-admin sibling of ``usePagedQuery`` (arch-r5 #6, 2026-07-11) —
 * the lone owner of the "loading + error + forbidden + refresh" state
 * machine that every ``/agent-mcp/api/router/*`` GET call-site hand-
 * rolled independently:
 *
 *   * ``sso-dashboard.tsx`` — GET ``/router/sso/config``
 *   * ``groups-dashboard.tsx`` — GET ``/router/groups`` (list) AND
 *     GET ``/router/groups/<id>/capabilities`` (per-group panel)
 *   * ``users-dashboard.tsx`` — GET ``/router/users``
 *   * ``project-memberships-modal.tsx`` — GET
 *     ``/router/projects/<name>/memberships``
 *
 * Each of these re-typed ``useState(loading) / useState(error) /
 * useState(forbidden)`` plus a ``useEffect(refresh)`` and a
 * ``catch (e) { e instanceof Error ? e.message : String(e) }``
 * idiom. Two of the four (SSO, group capabilities) additionally
 * special-cased ``e instanceof ApiError && e.status === 403`` to show
 * a "sysadmin only" card instead of a raw error string; the other two
 * (users list, groups list) plus the memberships modal did NOT — they
 * rendered the 403 as generic red error text.
 *
 * That inconsistency turned out to be a real gap, not a stylistic
 * one: the 2026-07-08 security fix in ``admin_users_api.py``
 * ("viewer-read-gating finding 2") gated the LIST reads
 * (``list_users`` / ``list_groups`` / ``list_group_members`` /
 * ``list_project_memberships``) on the SAME capability as their
 * sibling mutations — a 403 is a live, reachable response for every
 * one of these endpoints when a non-privileged operator loads the
 * page, not just for SSO/capabilities. Centralizing the fetch state
 * machine here folds ``ApiError.status === 403`` into ONE ``forbidden``
 * flag for every consumer, so every router-admin view gets the same
 * "sysadmin only" treatment uniformly instead of three of four
 * quietly dumping a raw error string at a plain viewer.
 *
 * Design choice — sibling hook, not a ``usePagedQuery`` option:
 * ``usePagedQuery`` is shaped around the paginated ``POST
 * {limit,offset,...filters}`` → ``{rows[], total}`` envelope used by
 * the per-project query surface. Router-admin endpoints don't share
 * that shape — SSO returns a single config object, capabilities
 * return a bare array with no ``total``, and callers span
 * GET/POST/PATCH/PUT/DELETE with no pagination at all. Bolting a
 * ``client`` + ``forbiddenOnStatus`` option onto ``usePagedQuery``
 * would leave both the pagination fields (``total``, ``limit``,
 * ``offset``) AND the 403 fold on every consumer regardless of
 * whether they need it. A sibling hook that reuses the SAME
 * AbortController race-guard pattern (see ``use-paged-query.ts``) but
 * owns a narrower ``{data, loading, error, forbidden}`` result is the
 * better long-term seam: each hook stays legible on its own, and a
 * future non-paginated per-project endpoint can reach for this one
 * too without router-specific baggage.
 *
 * ``forbidden`` is purely additive — a 403 does NOT special-case
 * ``error`` (it stays ``null``), matching the two components that
 * already had explicit 403 handling. Consumers that want the classic
 * "sysadmin only" card check ``forbidden`` before ``error`` (the same
 * order SSO's JSX already used); consumers that don't check
 * ``forbidden`` simply never render anything for the 403 case — this
 * is a deliberate behavior fix (see above), not an oversight.
 */
export interface UseRouterQueryOptions {
  /**
   * Extra dependencies that trigger a refetch, e.g. a group id or a
   * project name the fetcher closes over. Defaults to ``[]`` (fetch
   * once on mount).
   */
  deps?: ReadonlyArray<unknown>
  /**
   * Gate the fetch. When ``false``, no request is issued and the
   * hook stays idle. Mirrors ``project-memberships-modal.tsx``'s
   * "only fetch while the dialog is open" gate — the initial
   * ``loading`` value tracks ``enabled`` so a not-yet-open consumer
   * doesn't render a loading spinner it will never resolve. Defaults
   * to ``true``.
   */
  enabled?: boolean
}

export interface UseRouterQueryResult<T> {
  /** The last successful fetch's payload, or null before the first
   *  success (or after a 403 / error). */
  readonly data: T | null
  /** True while a fetch is in flight. */
  readonly loading: boolean
  /** The last non-403 error, or null. Real Error instance. */
  readonly error: Error | null
  /** True iff the last fetch failed with HTTP 403. */
  readonly forbidden: boolean
  /** Re-run the fetch. Stable identity. */
  refresh: () => void
}

/** The three outcomes a single router-admin fetch can resolve to,
 *  factored out as a plain async function (no React) so it's
 *  unit-testable the same way ``router-api-client.test.ts`` tests
 *  ``request()`` — a pure async call against a stubbed fetcher, no
 *  jsdom / React-testing-library needed (this project's Vitest
 *  harness is deliberately React-free; see ``vitest.config.ts``). */
export type RouterQueryOutcome<T> =
  | { kind: "success"; data: T }
  | { kind: "forbidden" }
  | { kind: "error"; error: Error }
  /** The fetch was superseded (aborted) — the caller must not write
   *  any state; a fresher request already owns the slot. */
  | { kind: "aborted" }

export async function resolveRouterQuery<T>(
  fetchFn: (signal: AbortSignal) => Promise<T>,
  signal: AbortSignal,
): Promise<RouterQueryOutcome<T>> {
  try {
    const data = await fetchFn(signal)
    if (signal.aborted) return { kind: "aborted" }
    return { kind: "success", data }
  } catch (e: unknown) {
    // AbortError surfaces as a DOMException with name === 'AbortError'
    // in browsers, or a regular Error in some polyfills — either way
    // an aborted fetch is a no-op, matching usePagedQuery's guard.
    if (signal.aborted) return { kind: "aborted" }
    if (e instanceof Error && e.name === "AbortError") return { kind: "aborted" }
    if (e instanceof ApiError && e.status === 403) {
      return { kind: "forbidden" }
    }
    const err = e instanceof Error ? e : new Error(String(e))
    return { kind: "error", error: err }
  }
}

export function useRouterQuery<T>(
  fetchFn: (signal: AbortSignal) => Promise<T>,
  options: UseRouterQueryOptions = {},
): UseRouterQueryResult<T> {
  const { deps = [], enabled = true } = options

  const [data, setData] = useState<T | null>(null)
  // Initial loading mirrors `enabled` — a disabled (e.g. "dialog not
  // open yet") consumer starts idle rather than flashing a spinner
  // for a fetch that will never fire.
  const [loading, setLoading] = useState<boolean>(enabled)
  const [error, setError] = useState<Error | null>(null)
  const [forbidden, setForbidden] = useState<boolean>(false)

  // Refs mirror use-paged-query.ts's race-guard: `abortRef` cancels a
  // stale in-flight fetch before a fresh one starts (or before the
  // effect's cleanup fires), `fetchFnRef` keeps `refresh()`'s identity
  // stable without forcing every caller to `useCallback` a fetchFn
  // with a matching dependency list on every render.
  const abortRef = useRef<AbortController | null>(null)
  const fetchFnRef = useRef(fetchFn)
  fetchFnRef.current = fetchFn

  const runFetch = useCallback(async (): Promise<void> => {
    if (abortRef.current) {
      abortRef.current.abort()
    }
    const controller = new AbortController()
    abortRef.current = controller
    const { signal } = controller

    setLoading(true)
    setError(null)
    setForbidden(false)

    const outcome = await resolveRouterQuery(fetchFnRef.current, signal)

    if (abortRef.current === controller) {
      abortRef.current = null
    }
    // A superseded request must not write state — the request that
    // replaced it already owns loading/error/forbidden.
    if (outcome.kind === "aborted") return

    switch (outcome.kind) {
      case "success":
        setData(outcome.data)
        break
      case "forbidden":
        setForbidden(true)
        break
      case "error":
        setError(outcome.error)
        break
    }
    setLoading(false)
  }, [])

  const refresh = useCallback(() => {
    void runFetch()
  }, [runFetch])

  useEffect(() => {
    if (!enabled) return
    void runFetch()
    return () => {
      if (abortRef.current) {
        abortRef.current.abort()
        abortRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, runFetch, ...deps])

  return { data, loading, error, forbidden, refresh }
}
