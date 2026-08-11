// Direct unit coverage for the data-store's read selectors and the
// fetch freshness gate — the reconcile/normalisation logic that the
// React tree relies on but that no component test exercises head-on.
//
//   * getAgent / getTask normalise the caller's id (strip the
//     `agent_` / `task_` routing prefix, collapse Admin↔admin) before
//     matching, so both the URL form and the raw store form resolve.
//   * fetchAllData is throttled: a non-forced call inside the 30s
//     freshness window must not hit the network.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest"
import { useDataStore } from "./data-store"
import { apiClient, type Agent, type Task } from "../api"

const agents = [
  { agent_id: "Admin", status: "running" },
  { agent_id: "worker1", status: "running" },
] as unknown as Agent[]

const tasks = [
  { task_id: "abc", title: "first", status: "pending" },
] as unknown as Task[]

function seed(overrides: Record<string, unknown> = {}) {
  useDataStore.setState({
    data: {
      agents,
      tasks,
      context: [],
      actions: [],
      file_metadata: [],
      file_map: {},
      timestamp: new Date().toISOString(),
    },
    loading: false,
    isRefreshing: false,
    error: null,
    lastFetch: Date.now(),
    ...overrides,
  })
}

describe("data-store selectors", () => {
  beforeEach(() => {
    seed()
  })

  it("getAgent strips the agent_ prefix and resolves Admin↔admin", () => {
    const { getAgent } = useDataStore.getState()
    expect(getAgent("worker1")?.agent_id).toBe("worker1")
    expect(getAgent("agent_worker1")?.agent_id).toBe("worker1")
    // Both casings + the prefix all land on the capitalised Admin row.
    expect(getAgent("admin")?.agent_id).toBe("Admin")
    expect(getAgent("Admin")?.agent_id).toBe("Admin")
    expect(getAgent("agent_Admin")?.agent_id).toBe("Admin")
    expect(getAgent("nope")).toBeUndefined()
  })

  it("getTask strips the task_ prefix", () => {
    const { getTask } = useDataStore.getState()
    expect(getTask("abc")?.task_id).toBe("abc")
    expect(getTask("task_abc")?.task_id).toBe("abc")
    expect(getTask("missing")).toBeUndefined()
  })
})

describe("data-store fetch freshness gate", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  const envelope = {
    agents,
    tasks,
    context: [],
    actions: [],
    file_metadata: [],
    file_map: {},
    timestamp: new Date().toISOString(),
  }

  it("skips the network when data is fresh (<30s) and not forced", async () => {
    seed({ lastFetch: Date.now() })
    const spy = vi
      .spyOn(apiClient, "getAllData")
      .mockResolvedValue(envelope as Awaited<ReturnType<typeof apiClient.getAllData>>)

    await useDataStore.getState().fetchAllData()
    expect(spy).not.toHaveBeenCalled()
  })

  it("fetches when forced even inside the freshness window", async () => {
    seed({ lastFetch: Date.now() })
    const spy = vi
      .spyOn(apiClient, "getAllData")
      .mockResolvedValue(envelope as Awaited<ReturnType<typeof apiClient.getAllData>>)

    await useDataStore.getState().fetchAllData(true)
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it("fetches when the cached data is stale (>30s old)", async () => {
    seed({ lastFetch: Date.now() - 40_000 })
    const spy = vi
      .spyOn(apiClient, "getAllData")
      .mockResolvedValue(envelope as Awaited<ReturnType<typeof apiClient.getAllData>>)

    await useDataStore.getState().fetchAllData()
    expect(spy).toHaveBeenCalledTimes(1)
  })
})
