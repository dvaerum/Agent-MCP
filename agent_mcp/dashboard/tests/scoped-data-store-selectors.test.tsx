// @vitest-environment jsdom
//
// Scoped selector hooks give each component a single-slice subscription.
// After Wave 6 (keystone increment 1) the envelope slices come from the
// shared `/all-data` TanStack Query (`useAgents`/`useTasks` in
// lib/queries/all-data.ts); `useSseHealthy` stays in the zustand
// data-store (the PF-3 flag is not part of the envelope). These tests
// pin two properties:
//   1. each hook returns the correct slice of the cache, and
//   2. the no-data path returns a STABLE empty reference (a fresh []
//      every render would defeat reference equality and force a
//      re-render on every unrelated write — the churn the scoped
//      selectors exist to prevent).
import { describe, it, expect, beforeEach, afterEach } from "vitest"
import React from "react"
import { renderHook, cleanup } from "@testing-library/react"
import { QueryClientProvider } from "@tanstack/react-query"
import { queryClient, allDataQueryKey } from "@/lib/query-client"
import { useAgents, useTasks } from "@/lib/queries/all-data"
import { useDataStore, useSseHealthy } from "@/lib/stores/data-store"
import { useServerStore } from "@/lib/stores/server-store"
import type { Agent, Task } from "@/lib/api"

const agents = [{ agent_id: "a1", status: "running" }] as unknown as Agent[]
const tasks = [{ task_id: "t1", title: "T", status: "pending" }] as unknown as Task[]

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

/** Seed the server-store so the query's `enabled` gate opens (only
 *  needed for the loaded case; the empty case leaves it disabled). */
function seedConnected() {
  useServerStore.setState({
    servers: [
      { id: "s1", name: "t", host: "h", port: 1, status: "connected" },
    ] as never,
    activeServerId: "s1",
  })
}

function seedEnvelope() {
  queryClient.setQueryData(allDataQueryKey(null), {
    agents,
    tasks,
    context: [],
    actions: [],
    file_metadata: [],
    file_map: {},
    timestamp: new Date().toISOString(),
  })
}

describe("scoped all-data selectors", () => {
  beforeEach(() => {
    queryClient.clear()
    useServerStore.setState({ servers: [], activeServerId: null })
    useDataStore.setState({ sseHealthy: false })
  })
  afterEach(() => cleanup())

  it("useAgents / useTasks return the loaded slices from the cache", () => {
    seedConnected()
    seedEnvelope()
    expect(renderHook(() => useAgents(), { wrapper }).result.current).toBe(agents)
    expect(renderHook(() => useTasks(), { wrapper }).result.current).toBe(tasks)
  })

  it("returns a STABLE empty ref for agents/tasks when no data is cached", () => {
    const a1 = renderHook(() => useAgents(), { wrapper }).result.current
    const a2 = renderHook(() => useAgents(), { wrapper }).result.current
    const t1 = renderHook(() => useTasks(), { wrapper }).result.current
    const t2 = renderHook(() => useTasks(), { wrapper }).result.current
    expect(a1).toHaveLength(0)
    expect(a1).toBe(a2) // same frozen singleton, not a fresh []
    expect(t1).toBe(t2)
  })

  it("useSseHealthy reflects the data-store flag", () => {
    useDataStore.setState({ sseHealthy: true })
    expect(renderHook(() => useSseHealthy()).result.current).toBe(true)
  })
})
