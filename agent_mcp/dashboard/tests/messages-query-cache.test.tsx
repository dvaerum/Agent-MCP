// @vitest-environment jsdom
/**
 * W6-followup F3 — messages list on TanStack Query: cache-serving guard.
 *
 * The messages list fetch is `useMessagesQuery` over the shared
 * `queryClient` (`['messages', project, {filters, limit, offset}]`). This
 * pins that a mounted query serves the page from the cache — a component
 * (re)mount inside the freshness window reuses the cached page instead of
 * re-hitting `POST /messages/query`, the "single source" property the
 * migration exists to deliver (the equivalent of the in-place cache the
 * retired `usePagedQuery` path kept).
 *
 * Unlike tasks, messages is SERVER-paginated: `offset`/`limit` are part
 * of the key (each page its own backend round-trip), so this also pins
 * that the fetch actually POSTs to `/messages/query`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest"
import React from "react"
import { renderHook, cleanup, waitFor } from "@testing-library/react"
import { QueryClientProvider } from "@tanstack/react-query"
import { queryClient, messagesQueryKey } from "@/lib/query-client"
import { useMessagesQuery } from "@/lib/queries/messages"
import { useDataStore } from "@/lib/stores/data-store"
import { useServerStore } from "@/lib/stores/server-store"
import { apiClient, type MessagesPage } from "@/lib/api"
import { projectContext } from "@/lib/project-context"

const cachedPage: MessagesPage = {
  messages: [{ message_id: "m1", subject: "cached" }] as unknown as MessagesPage["messages"],
  total: 1,
}

const LIMIT = 100
const OFFSET = 0

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

describe("useMessagesQuery cache-serving", () => {
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

  it("serves the cached page without re-hitting POST /messages/query", () => {
    const request = vi
      .spyOn(apiClient, "request")
      .mockResolvedValue({ messages: [], total: 0 } as never)
    seedConnected()

    // A prior fetch already populated the cache for this exact page
    // window; the write marks it fresh (inside staleTime).
    const filters = { read: false }
    queryClient.setQueryData(
      messagesQueryKey(projectContext.projectName, {
        filters,
        limit: LIMIT,
        offset: OFFSET,
      }),
      cachedPage,
    )

    const { result } = renderHook(
      () => useMessagesQuery(filters, LIMIT, OFFSET),
      { wrapper },
    )

    // Served straight from cache — same reference, no network call.
    expect(result.current.data).toBe(cachedPage)
    expect(request).not.toHaveBeenCalled()
  })

  it("fetches via POST /messages/query on a cache miss (new page)", async () => {
    const request = vi
      .spyOn(apiClient, "request")
      .mockResolvedValue({
        messages: [{ message_id: "m2" }],
        total: 1,
      } as never)
    seedConnected()

    // A different offset than the seeded one → cache miss → fetch.
    const { result } = renderHook(
      () => useMessagesQuery({ read: true }, LIMIT, 100),
      { wrapper },
    )

    await waitFor(() => expect(result.current.data?.total).toBe(1))
    expect(request).toHaveBeenCalled()
    const [endpoint, opts] = request.mock.calls[0]!
    expect(endpoint).toBe("/messages/query")
    expect((opts as { method?: string } | undefined)?.method).toBe("POST")
  })

  it("stays disabled (no fetch) while no server is connected", () => {
    const request = vi
      .spyOn(apiClient, "request")
      .mockResolvedValue({ messages: [], total: 0 } as never)
    // server-store left disconnected by beforeEach.
    renderHook(() => useMessagesQuery({}, LIMIT, OFFSET), { wrapper })
    expect(request).not.toHaveBeenCalled()
  })
})
