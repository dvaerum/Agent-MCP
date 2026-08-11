import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import {
  apiClient,
  contextEntryToMemory,
  type RawContextEntry,
} from "@/lib/api"

// W6-followup F1 (ST-5): the raw-context → Memory mapper + the
// getMemories read path. Two bugs are pinned here:
//   1. the mapper crashed on a row whose `value` was `undefined`
//      (`JSON.stringify(undefined)` → undefined → `.length` throws),
//   2. getMemories accepted a filter/sort/limit options object but
//      IGNORED it entirely, always returning the full list.

describe("contextEntryToMemory (ST-5 mapper)", () => {
  const base: RawContextEntry = {
    context_key: "k",
    value: { a: 1 },
    updated_at: new Date().toISOString(),
    updated_by: "admin",
  }

  it("does NOT crash on a row whose value is undefined", () => {
    // RED before the fix: `JSON.stringify(undefined).length` throws
    // "Cannot read properties of undefined (reading 'length')".
    const out = contextEntryToMemory({ ...base, value: undefined })
    expect(out._metadata?.size_bytes).toBe(0)
    expect(out._metadata?.is_large).toBe(false)
  })

  it("computes size from the serialized value", () => {
    const out = contextEntryToMemory({ ...base, value: "hi" })
    // JSON.stringify("hi") === '"hi"' → 4 chars.
    expect(out._metadata?.size_bytes).toBe(4)
  })

  it("flags a large value", () => {
    const out = contextEntryToMemory({ ...base, value: "x".repeat(11000) })
    expect(out._metadata?.is_large).toBe(true)
  })

  it("flags a stale row (updated > 30 days ago)", () => {
    const old = new Date(Date.now() - 40 * 24 * 60 * 60 * 1000).toISOString()
    const out = contextEntryToMemory({ ...base, updated_at: old })
    expect(out._metadata?.is_stale).toBe(true)
  })

  it("does not flag a fresh row as stale", () => {
    const out = contextEntryToMemory(base)
    expect(out._metadata?.is_stale).toBe(false)
  })

  it("preserves the ownership + identity columns", () => {
    const out = contextEntryToMemory({
      ...base,
      description: "d",
      created_at: "2026-01-01",
      created_by: "agent-1",
    })
    expect(out.context_key).toBe("k")
    expect(out.description).toBe("d")
    expect(out.created_by).toBe("agent-1")
  })
})

describe("apiClient.getMemories (ST-5 options)", () => {
  const realFetch = global.fetch

  const rows: RawContextEntry[] = [
    { context_key: "alpha", value: "short", updated_at: "2026-01-03T00:00:00Z", updated_by: "admin", description: "first note" },
    { context_key: "beta", value: "a much longer value here", updated_at: "2026-01-01T00:00:00Z", updated_by: "admin" },
    { context_key: "gamma", value: undefined, updated_at: "2026-01-02T00:00:00Z", updated_by: "admin", description: "needle" },
  ]

  function stubAllData() {
    global.fetch = vi.fn(async () =>
      ({
        ok: true,
        status: 200,
        statusText: "OK",
        text: async () => "",
        json: async () => ({ context: rows }),
      }) as unknown as Response,
    ) as unknown as typeof fetch
  }

  beforeEach(() => {
    apiClient.setBaseUrl("/api")
    stubAllData()
  })

  afterEach(() => {
    global.fetch = realFetch
    vi.restoreAllMocks()
  })

  it("returns every row (mapped, no crash on undefined value) with no options", async () => {
    const out = await apiClient.getMemories()
    expect(out).toHaveLength(3)
    expect(out.map((m) => m.context_key).sort()).toEqual(["alpha", "beta", "gamma"])
  })

  it("filters by exact context_key", async () => {
    const out = await apiClient.getMemories({ context_key: "beta" })
    expect(out).toHaveLength(1)
    expect(out[0].context_key).toBe("beta")
  })

  it("filters by search_query over key / description / value", async () => {
    // 'needle' only appears in gamma's description.
    const byDesc = await apiClient.getMemories({ search_query: "needle" })
    expect(byDesc.map((m) => m.context_key)).toEqual(["gamma"])
    // 'longer' only appears in beta's value.
    const byValue = await apiClient.getMemories({ search_query: "longer" })
    expect(byValue.map((m) => m.context_key)).toEqual(["beta"])
  })

  it("sorts by key", async () => {
    const out = await apiClient.getMemories({ sort_by: "key" })
    expect(out.map((m) => m.context_key)).toEqual(["alpha", "beta", "gamma"])
  })

  it("sorts by updated_at (newest first)", async () => {
    const out = await apiClient.getMemories({ sort_by: "updated_at" })
    expect(out.map((m) => m.context_key)).toEqual(["alpha", "gamma", "beta"])
  })

  it("caps results with max_results", async () => {
    const out = await apiClient.getMemories({ sort_by: "key", max_results: 2 })
    expect(out.map((m) => m.context_key)).toEqual(["alpha", "beta"])
  })
})
