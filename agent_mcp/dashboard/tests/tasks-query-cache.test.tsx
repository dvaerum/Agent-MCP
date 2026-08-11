// @vitest-environment jsdom
/**
 * W6-followup F2 — tasks list on TanStack Query: cache-serving guard.
 *
 * The tasks list fetch is `useTasksQuery` over the shared `queryClient`
 * (`['tasks', project, filters]`). This pins that a mounted query serves
 * the row set from the cache — a component (re)mount inside the freshness
 * window reuses the cached list instead of re-hitting `GET /tasks`, which
 * is the "single source" property the migration exists to deliver (and
 * the equivalent of the 30s cache the retired `usePagedQuery` path ran).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest"
import React from "react"
import { renderHook, cleanup, waitFor } from "@testing-library/react"
import { QueryClientProvider } from "@tanstack/react-query"
import { queryClient, tasksQueryKey } from "@/lib/query-client"
import { useTasksQuery } from "@/lib/queries/tasks"
import { useDataStore } from "@/lib/stores/data-store"
import { useServerStore } from "@/lib/stores/server-store"
import { apiClient, type Task } from "@/lib/api"
import { projectContext } from "@/lib/project-context"

const cachedTasks = [
  { task_id: "t1", title: "cached", status: "pending" },
] as unknown as Task[]

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

function seedConnected() {
  useServerStore.setState({
    servers: [
      { id: "s1", name: "t", host: "h", port: 1, status: "connected" },
    ] as never,
    activeServerId: "s1",
  })
}

describe("useTasksQuery cache-serving", () => {
  beforeEach(() => {
    queryClient.clear()
    useServerStore.setState({ servers: [], activeServerId: null })
    // SSE healthy so the fallback poll is suppressed and can't muddy the
    // "no fetch" assertion.
    useDataStore.setState({ sseHealthy: true })
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("serves the cached list without re-hitting GET /tasks", async () => {
    const getTasks = vi
      .spyOn(apiClient, "getTasks")
      .mockResolvedValue([] as Task[])
    seedConnected()

    // A prior fetch already populated the cache for this exact filter
    // snapshot; the write marks it fresh (inside staleTime).
    const filters = { status: "pending" }
    queryClient.setQueryData(
      tasksQueryKey(projectContext.projectName, filters),
      cachedTasks,
    )

    const { result } = renderHook(() => useTasksQuery(filters), { wrapper })

    // Served straight from cache — same reference, no network call.
    expect(result.current.data).toBe(cachedTasks)
    expect(getTasks).not.toHaveBeenCalled()
  })

  it("fetches via apiClient.getTasks on a cache miss (new filter)", async () => {
    const fetched = [
      { task_id: "t2", title: "fetched", status: "completed" },
    ] as unknown as Task[]
    const getTasks = vi
      .spyOn(apiClient, "getTasks")
      .mockResolvedValue(fetched)
    seedConnected()

    const filters = { status: "completed" }
    const { result } = renderHook(() => useTasksQuery(filters), { wrapper })

    await waitFor(() => expect(result.current.data).toBe(fetched))
    expect(getTasks).toHaveBeenCalledWith(filters)
  })

  it("stays disabled (no fetch) while no server is connected", () => {
    const getTasks = vi
      .spyOn(apiClient, "getTasks")
      .mockResolvedValue([] as Task[])
    // server-store left disconnected by beforeEach.
    renderHook(() => useTasksQuery({}), { wrapper })
    expect(getTasks).not.toHaveBeenCalled()
  })
})
