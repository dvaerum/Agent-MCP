// Unit coverage for the data-store's remaining responsibilities after
// Wave 6 keystone increment 1 moved the `/all-data` server-cache onto
// TanStack Query. What's left in this zustand store:
//
//   * the prompt-book catalogue slice (separate endpoint + cadence),
//     including the load skip-gate and the `tags` normalisation the
//     2026-06-17 Firefox click-through exposed, and
//   * the PF-3 `sseHealthy` flag, which the all-data query reads to gate
//     its fallback poll — flipped only on an actual change.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest"
import { useDataStore } from "./data-store"
import { apiClient } from "../api"

const catalogEnvelope = {
  categories: [{ id: "c1", name: "Cat", description: "", icon: "" }],
  prompts: [
    // One prompt deliberately WITHOUT `tags` to pin the boundary
    // normalisation (the catalogue drift that threw
    // `TypeError: s.tags is undefined`).
    {
      id: "p1",
      title: "P1",
      description: "",
      category: "c1",
      template: "",
      variables: [],
      usage: "",
    },
  ],
} as unknown as Awaited<ReturnType<typeof apiClient.getPromptsCatalog>>

describe("data-store prompts catalogue slice", () => {
  beforeEach(() => {
    useDataStore.setState({
      promptsCatalog: null,
      promptsCategories: null,
      promptsCatalogLoading: false,
    })
  })
  afterEach(() => vi.restoreAllMocks())

  it("fetches the catalogue and backfills missing tags to []", async () => {
    const spy = vi
      .spyOn(apiClient, "getPromptsCatalog")
      .mockResolvedValue(catalogEnvelope)

    await useDataStore.getState().fetchPromptsCatalog()
    expect(spy).toHaveBeenCalledTimes(1)
    const state = useDataStore.getState()
    expect(state.promptsCatalog?.[0]?.tags).toEqual([])
    expect(state.promptsCategories?.[0]?.id).toBe("c1")
  })

  it("skips the network when the catalogue is already loaded and not forced", async () => {
    useDataStore.setState({ promptsCatalog: [], promptsCategories: [] })
    const spy = vi
      .spyOn(apiClient, "getPromptsCatalog")
      .mockResolvedValue(catalogEnvelope)

    await useDataStore.getState().fetchPromptsCatalog()
    expect(spy).not.toHaveBeenCalled()
  })

  it("invalidatePromptsCatalog clears + refetches", async () => {
    useDataStore.setState({ promptsCatalog: [], promptsCategories: [] })
    const spy = vi
      .spyOn(apiClient, "getPromptsCatalog")
      .mockResolvedValue(catalogEnvelope)

    useDataStore.getState().invalidatePromptsCatalog()
    // The clear happens synchronously; the refetch is scheduled.
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
  })
})

describe("data-store sseHealthy flag (PF-3)", () => {
  it("writes only on an actual flip", () => {
    useDataStore.setState({ sseHealthy: false })
    const seen: boolean[] = []
    const unsub = useDataStore.subscribe((s) => seen.push(s.sseHealthy))

    const { setSseHealthy } = useDataStore.getState()
    setSseHealthy(false) // no-op — already false
    setSseHealthy(true) // flip
    setSseHealthy(true) // no-op — already true
    setSseHealthy(false) // flip

    unsub()
    expect(seen).toEqual([true, false])
  })
})
