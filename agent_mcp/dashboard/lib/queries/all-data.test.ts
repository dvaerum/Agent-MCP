// Pure-function coverage for the `/all-data` query module's selectors.
//
// These are the reconcile/normalisation helpers the React tree relies on
// but that no component test exercises head-on — ported from the old
// data-store selector tests (getAgent / getAgentTasks) when the envelope
// moved onto TanStack Query (Wave 6 keystone increment 1):
//
//   * selectAgent normalises the caller's id (strip the `agent_`
//     routing prefix, collapse Admin↔admin) before matching, so both
//     the URL form and the raw store form resolve.
//   * selectAgentTasks unions "assigned to this agent" with "worked on
//     by this agent" (derived from the actions slice), deduped.
import { describe, it, expect } from "vitest"
import { selectAgent, selectAgentTasks, type AllData } from "./all-data"
import type { Agent, Task } from "../api"

const agents = [
  { agent_id: "Admin", status: "running" },
  { agent_id: "worker1", status: "running" },
] as unknown as Agent[]

const tasks = [
  { task_id: "t1", title: "assigned", status: "pending", assigned_to: "worker1" },
  { task_id: "t2", title: "worked", status: "in_progress", assigned_to: "worker2" },
  { task_id: "t3", title: "other", status: "pending", assigned_to: "worker2" },
] as unknown as Task[]

const actions = [
  { agent_id: "worker1", task_id: "t2", action_type: "note" },
]

const data: AllData = {
  agents,
  tasks,
  context: [],
  actions,
  file_metadata: [],
  file_map: {},
  timestamp: new Date().toISOString(),
}

describe("selectAgent", () => {
  it("strips the agent_ prefix and resolves Admin↔admin", () => {
    expect(selectAgent(data, "worker1")?.agent_id).toBe("worker1")
    expect(selectAgent(data, "agent_worker1")?.agent_id).toBe("worker1")
    // Both casings + the prefix all land on the capitalised Admin row.
    expect(selectAgent(data, "admin")?.agent_id).toBe("Admin")
    expect(selectAgent(data, "Admin")?.agent_id).toBe("Admin")
    expect(selectAgent(data, "agent_Admin")?.agent_id).toBe("Admin")
    expect(selectAgent(data, "nope")).toBeUndefined()
  })

  it("returns undefined for an empty envelope", () => {
    expect(selectAgent(undefined, "worker1")).toBeUndefined()
  })
})

describe("selectAgentTasks", () => {
  it("unions assigned tasks with worked-on tasks, deduped", () => {
    const result = selectAgentTasks(data, "worker1")
    const ids = result.map((t) => t.task_id).sort()
    // t1 assigned to worker1; t2 worked on (action) but assigned to
    // worker2; t3 belongs to worker2 only and must not appear.
    expect(ids).toEqual(["t1", "t2"])
  })

  it("returns an empty list for an empty envelope", () => {
    expect(selectAgentTasks(undefined, "worker1")).toEqual([])
  })
})
