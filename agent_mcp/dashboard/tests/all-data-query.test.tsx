// @vitest-environment jsdom
//
// Wave 6 keystone increment 1 — the `/all-data` envelope on TanStack
// Query. Two properties the migration must hold:
//
//   1. The list hooks (`useAgents` et al) serve their slice from the ONE
//      shared `['all-data', project]` query cache — a single fetch feeds
//      every consumer (fixes ST-3 double-sourcing).
//   2. An operator-events `resources/updated` notification triggers
//      EXACTLY ONE invalidation of that query, even for a burst of
//      notifications (the 300ms debounce coalesces them) — one source,
//      one refetch, no split-brain (fixes ST-4). W6-followup F2 added a
//      sibling `invalidateTasks()` on the SAME debounced tick (the tasks
//      list is a separate `['tasks', project]` query), so the choke
//      point now fires one invalidation PER QUERY per burst — this guard
//      pins the coalescing on the all-data key specifically (still
//      exactly one, never one-per-notification).
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest"
import React from "react"
import { renderHook, waitFor, cleanup } from "@testing-library/react"
import { QueryClientProvider } from "@tanstack/react-query"
import { queryClient } from "@/lib/query-client"
import {
  selectAgent,
  selectAgentTasks,
  useAgents,
  useAllDataQuery,
  type AllData,
} from "@/lib/queries/all-data"
import { dispatchNotification } from "@/lib/mcp-notifications"
import { useServerStore } from "@/lib/stores/server-store"
import { apiClient, type Agent } from "@/lib/api"

const envelope = {
  agents: [{ agent_id: "worker1", status: "running" }] as unknown as Agent[],
  tasks: [],
  context: [],
  actions: [],
  file_metadata: [],
  file_map: {},
  timestamp: new Date().toISOString(),
}

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

/** Seed the server-store so the query's `enabled` gate opens. */
function seedConnected() {
  useServerStore.setState({
    servers: [
      {
        id: "s1",
        name: "test",
        host: "localhost",
        port: 8080,
        status: "connected",
      },
    ] as never,
    activeServerId: "s1",
  })
}

afterEach(() => {
  cleanup()
  queryClient.clear()
  vi.restoreAllMocks()
  useServerStore.setState({ servers: [], activeServerId: null })
})

describe("all-data query cache", () => {
  beforeEach(() => seedConnected())

  it("serves the agents slice from the shared query cache after one fetch", async () => {
    const spy = vi
      .spyOn(apiClient, "getAllData")
      .mockResolvedValue(
        envelope as Awaited<ReturnType<typeof apiClient.getAllData>>,
      )

    const { result } = renderHook(() => useAgents(), { wrapper })

    await waitFor(() => expect(result.current).toHaveLength(1))
    expect(result.current[0]!.agent_id).toBe("worker1")
    // A second consumer of the same key reuses the cache — no new fetch.
    renderHook(() => useAllDataQuery(), { wrapper })
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
  })
})

// AUDIT AF-A: selectAgent / selectAgentTasks are reachable imperatively
// (getAgentTokenCached → prompt-book Run handler) with whatever snapshot
// the cache holds. A malformed/empty envelope must yield `undefined` / an
// empty list, never a `Cannot read properties of undefined` TypeError.
describe("selectAgent / selectAgentTasks robustness (AF-A)", () => {
  it("returns undefined on an undefined envelope", () => {
    expect(selectAgent(undefined, "worker1")).toBeUndefined()
  })

  it("returns undefined (not throw) on an envelope missing agents", () => {
    // A 200 whose body lost the agents array (backend renamed the field):
    // the old `data.agents.find(...)` would TypeError here.
    const malformed = { tasks: [], context: [] } as unknown as AllData
    expect(() => selectAgent(malformed, "worker1")).not.toThrow()
    expect(selectAgent(malformed, "worker1")).toBeUndefined()
    // The Admin-casing branch is guarded too.
    expect(selectAgent(malformed, "admin")).toBeUndefined()
  })

  it("still resolves an agent from a well-shaped envelope", () => {
    const env = {
      agents: [{ agent_id: "worker1" }, { agent_id: "Admin" }],
      tasks: [],
      context: [],
      actions: [],
    } as unknown as AllData
    expect(selectAgent(env, "worker1")!.agent_id).toBe("worker1")
    // agent_ prefix + Admin/admin casing tolerance survives the hardening.
    expect(selectAgent(env, "agent_worker1")!.agent_id).toBe("worker1")
    expect(selectAgent(env, "admin")!.agent_id).toBe("Admin")
  })

  it("returns [] (not throw) on an envelope missing tasks/actions", () => {
    const malformed = { agents: [] } as unknown as AllData
    expect(() => selectAgentTasks(malformed, "worker1")).not.toThrow()
    expect(selectAgentTasks(malformed, "worker1")).toEqual([])
  })
})

describe("SSE → single invalidation (ST-4)", () => {
  beforeEach(() => seedConnected())

  it("coalesces a burst of resources/updated into ONE invalidation", async () => {
    vi.useFakeTimers()
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries")

    // A burst of notifications (as an active project emits — one per
    // mutation) arrives inside the debounce window.
    dispatchNotification({
      method: "notifications/resources/updated",
      params: { uri: "agent-mcp://inbox/worker1" },
    })
    dispatchNotification({
      method: "notifications/resources/updated",
      params: { uri: "agent-mcp://status/worker1" },
    })
    dispatchNotification({
      method: "notifications/resources/updated",
      params: { uri: "agent-mcp://inbox/worker2" },
    })

    // Nothing yet — the debounce hasn't elapsed.
    expect(invalidateSpy).not.toHaveBeenCalled()

    // After the 300ms debounce, the all-data key is invalidated exactly
    // once for the whole burst (not one per notification). W6-followup
    // F2 also invalidates the sibling `['tasks', project]` key on the
    // same tick, so we count all-data invalidations specifically rather
    // than the total call count.
    vi.advanceTimersByTime(300)
    const allDataInvalidations = invalidateSpy.mock.calls.filter(
      ([arg]) =>
        Array.isArray((arg as { queryKey?: unknown[] })?.queryKey) &&
        (arg as { queryKey: unknown[] }).queryKey[0] === "all-data",
    )
    expect(allDataInvalidations).toHaveLength(1)

    vi.useRealTimers()
  })
})
