// @vitest-environment jsdom
/**
 * Schedules list on TanStack Query: cache-serving guard.
 *
 * The schedules list fetch is `useSchedulesQuery` over the shared
 * `queryClient` (`['schedules', project]`). This pins that a mounted
 * query serves the row set from the cache — a component (re)mount inside
 * the freshness window reuses the cached list instead of re-hitting
 * `GET /schedules` — mirrors `tests/tasks-query-cache.test.tsx`, adapted
 * for `useSchedulesQuery`'s no-filters key shape.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest"
import React from "react"
import { renderHook, cleanup, waitFor } from "@testing-library/react"
import { QueryClientProvider } from "@tanstack/react-query"
import { queryClient, schedulesQueryKey } from "@/lib/query-client"
import { useSchedulesQuery } from "@/lib/queries/schedules"
import { useDataStore } from "@/lib/stores/data-store"
import { useServerStore } from "@/lib/stores/server-store"
import { apiClient, type Schedule } from "@/lib/api"
import { projectContext } from "@/lib/project-context"

const cachedSchedules = [
  { directive_id: "sd_1", agent_id: "alice", prompt: "cached" },
] as unknown as Schedule[]

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

describe("useSchedulesQuery cache-serving", () => {
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

  it("serves the cached list without re-hitting GET /schedules", async () => {
    const getSchedules = vi
      .spyOn(apiClient, "getSchedules")
      .mockResolvedValue([] as Schedule[])
    seedConnected()

    // A prior fetch already populated the cache for this project; the
    // write marks it fresh (inside staleTime).
    queryClient.setQueryData(
      schedulesQueryKey(projectContext.projectName),
      cachedSchedules,
    )

    const { result } = renderHook(() => useSchedulesQuery(), { wrapper })

    // Served straight from cache — same reference, no network call.
    expect(result.current.data).toBe(cachedSchedules)
    expect(getSchedules).not.toHaveBeenCalled()
  })

  it("fetches via apiClient.getSchedules on a cache miss", async () => {
    const fetched = [
      { directive_id: "sd_2", agent_id: "bob", prompt: "fetched" },
    ] as unknown as Schedule[]
    const getSchedules = vi
      .spyOn(apiClient, "getSchedules")
      .mockResolvedValue(fetched)
    seedConnected()

    const { result } = renderHook(() => useSchedulesQuery(), { wrapper })

    await waitFor(() => expect(result.current.data).toBe(fetched))
    expect(getSchedules).toHaveBeenCalled()
  })

  it("stays disabled (no fetch) while no server is connected", () => {
    const getSchedules = vi
      .spyOn(apiClient, "getSchedules")
      .mockResolvedValue([] as Schedule[])
    // server-store left disconnected by beforeEach.
    renderHook(() => useSchedulesQuery(), { wrapper })
    expect(getSchedules).not.toHaveBeenCalled()
  })
})
