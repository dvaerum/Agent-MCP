"use client"

import { useCallback, useMemo, useState } from "react"

/**
 * Generic filter state machine — the lone owner of the
 * "useState-per-filter-field + per-field updater + clearAll + filter-
 * watching effect" quartet that messages-dashboard.tsx, tasks-
 * dashboard.tsx, and agents-dashboard.tsx each used to hand-roll.
 *
 * PR 4 of the 2026-06-09 architecture review series. The three
 * dashboards previously declared their filter state like this:
 *
 *     // messages-dashboard.tsx (6 fields + reset effect)
 *     const [filters, setFilters] = useState<Filters>({from:"", to:"", …})
 *     useEffect(() => setCurrentOffset(0), [filters])    // v5.0.26
 *     const clearFilters = () => setFilters({from:"", to:"", …})
 *
 *     // tasks-dashboard.tsx (3 sibling useStates)
 *     const [searchTerm, setSearchTerm] = useState("")
 *     const [statusFilter, setStatusFilter] = useState<string>("all")
 *     const [priorityFilter, setPriorityFilter] = useState<string>("all")
 *
 *     // agents-dashboard.tsx (2 sibling useStates)
 *     const [searchTerm, setSearchTerm] = useState("")
 *     const [statusFilter, setStatusFilter] = useState<string>("all")
 *
 * The pattern was identical; the modules didn't share it. This hook
 * owns it. Consumers pick the field set as the generic parameter T,
 * pass the initial snapshot (which doubles as the clearAll target
 * AND the isActive baseline), and optionally pass an onReset callback
 * that runs whenever a filter changes — used by messages-dashboard.tsx
 * to reset the pagination cursor to page 1, the behaviour previously
 * encoded in the dedicated ``useEffect(() => setCurrentOffset(0),
 * [filters])`` block.
 *
 * Usage:
 *
 *     interface Filters { from: string; to: string; q: string }
 *     const { filters, setFilter, clearAll, isActive } = useFilters<Filters>({
 *       initial: { from: "", to: "", q: "" },
 *       onReset: () => setCurrentOffset(0),  // optional
 *     })
 *
 *     // Single-field update — fires onReset.
 *     setFilter("from", "agent-1")
 *
 *     // Reset to initial — fires onReset.
 *     clearAll()
 *
 *     // Conditionally render a "Clear filters" button.
 *     {isActive && <Button onClick={clearAll}>Clear filters</Button>}
 *
 * Design notes:
 *
 *   * ``setFilter`` is the only mutator. Per-field setters (the kind
 *     tasks-dashboard.tsx used to expose as ``setSearchTerm`` etc.) are
 *     intentionally not returned — the JSX call-sites read just as well
 *     with ``onValueChange={(v) => setFilter("status", v)}`` and the
 *     single mutator keeps the hook's surface small.
 *
 *   * ``onReset`` is fired by BOTH ``setFilter`` and ``clearAll`` — i.e.
 *     any user-driven change to the filter snapshot. This is the
 *     filter-changed-reset-pagination semantics messages-dashboard.tsx
 *     needs. Consumers that don't need it pass no callback (the default
 *     is a no-op).
 *
 *   * ``isActive`` compares ``filters`` to ``initial`` via
 *     ``JSON.stringify``. This is sufficient for primitive-only filter
 *     shapes — which is what all three consumers use (string fields,
 *     string-union fields, no Dates / Sets / nested objects). If a
 *     future consumer needs a non-primitive shape, swap the strategy
 *     here (e.g. shallow deep-equal) without changing the hook's
 *     contract. JSON.stringify is order-stable for object literals with
 *     identical key order, which is the case here because we always
 *     spread ``initial`` first.
 *
 *   * The generic constraint is the open-ended ``object`` rather than
 *     ``Record<string, unknown>``. The latter would reject any
 *     interface (e.g. ``interface Filters { from: string; … }``)
 *     because TypeScript treats ``interface``s as not having an
 *     implicit string index signature — and the three call-sites
 *     would all have to rewrite their filter type as a ``type`` alias
 *     with explicit index. The ``object`` bound accepts both shapes
 *     and the per-field value type is still preserved via the
 *     ``<K extends keyof T>`` parameter on ``setFilter``.
 */
export function useFilters<T extends object>({
  initial,
  onReset,
}: {
  /** Start state. Also the target of `clearAll` and the baseline for `isActive`. */
  initial: T
  /**
   * Optional callback fired on every filter change (both `setFilter`
   * and `clearAll`). messages-dashboard.tsx uses this to reset the
   * pagination cursor; tasks- and agents-dashboard don't pass one.
   */
  onReset?: () => void
}): {
  /** The current filter snapshot. */
  readonly filters: T
  /**
   * Update a single filter field. Fires `onReset` after the update so
   * dependent state (e.g. pagination cursors) can react.
   */
  setFilter: <K extends keyof T>(key: K, value: T[K]) => void
  /**
   * Reset every filter field back to `initial`. Fires `onReset` so
   * dependent state can react.
   */
  clearAll: () => void
  /**
   * True iff at least one filter field differs from `initial` (i.e. the
   * user has narrowed the view in some way). Useful for conditionally
   * rendering a "Clear filters" button.
   */
  readonly isActive: boolean
} {
  const [filters, setFilters] = useState<T>(initial)

  // Stable identity so consumers can pass it down to memoised children
  // without busting their memo on every render.
  const setFilter = useCallback(
    <K extends keyof T>(key: K, value: T[K]) => {
      setFilters((prev) => ({ ...prev, [key]: value }))
      onReset?.()
    },
    [onReset],
  )

  const clearAll = useCallback(() => {
    setFilters(initial)
    onReset?.()
    // `initial` is treated as a stable contract value by callers (they
    // pass an object literal; React renders re-create it, but its
    // contents don't change for the life of the component). We
    // intentionally re-bind clearAll if it ever does change so the
    // reset target stays in sync.
  }, [initial, onReset])

  // JSON.stringify is the documented strategy for the primitive-only
  // filter shapes all three current consumers use; see the design
  // notes above. Memoised against both `filters` and `initial` so
  // hot renders don't pay the stringify cost when nothing changed.
  const isActive = useMemo(
    () => JSON.stringify(filters) !== JSON.stringify(initial),
    [filters, initial],
  )

  return {
    filters,
    setFilter,
    clearAll,
    isActive,
  }
}
