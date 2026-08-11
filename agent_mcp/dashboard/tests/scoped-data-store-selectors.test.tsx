// @vitest-environment jsdom
//
// PF-4: the scoped selector hooks give each component a single-slice
// subscription. These tests pin two properties:
//   1. each hook returns the correct slice of store state, and
//   2. the null-data path returns a STABLE empty reference (a fresh []
//      every render would defeat zustand's reference equality and force
//      a re-render on every unrelated store write — the exact churn the
//      scoped selectors exist to prevent).
//
// Lives under tests/ (not lib/) because vitest.config.ts includes
// `lib/**/*.test.ts` but not `.test.tsx`; the jsdom docblock + renderHook
// need the tsx path, which `tests/**/*.test.tsx` covers.
import { describe, it, expect, beforeEach } from "vitest"
import { renderHook } from "@testing-library/react"
import {
  useDataStore,
  useAgents,
  useTasks,
  useDataLoading,
  useIsRefreshing,
  useDataError,
  useSseHealthy,
} from "@/lib/stores/data-store"
import type { Agent, Task } from "@/lib/api"

const agents = [{ agent_id: "a1", status: "running" }] as unknown as Agent[]
const tasks = [{ task_id: "t1", title: "T", status: "pending" }] as unknown as Task[]

function seedEnvelope() {
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
  })
}

describe("scoped data-store selectors (PF-4)", () => {
  beforeEach(() => {
    useDataStore.setState({
      data: null,
      loading: false,
      isRefreshing: false,
      error: null,
      sseHealthy: false,
    })
  })

  it("useAgents / useTasks return the loaded slices", () => {
    seedEnvelope()
    expect(renderHook(() => useAgents()).result.current).toBe(agents)
    expect(renderHook(() => useTasks()).result.current).toBe(tasks)
  })

  it("returns a STABLE empty ref for agents/tasks when data is null", () => {
    const a1 = renderHook(() => useAgents()).result.current
    const a2 = renderHook(() => useAgents()).result.current
    const t1 = renderHook(() => useTasks()).result.current
    const t2 = renderHook(() => useTasks()).result.current
    expect(a1).toHaveLength(0)
    expect(a1).toBe(a2) // same frozen singleton, not a fresh []
    expect(t1).toBe(t2)
  })

  it("primitive selectors reflect their slice", () => {
    useDataStore.setState({
      loading: true,
      isRefreshing: true,
      error: "boom",
      sseHealthy: true,
    })
    expect(renderHook(() => useDataLoading()).result.current).toBe(true)
    expect(renderHook(() => useIsRefreshing()).result.current).toBe(true)
    expect(renderHook(() => useDataError()).result.current).toBe("boom")
    expect(renderHook(() => useSseHealthy()).result.current).toBe(true)
  })
})
