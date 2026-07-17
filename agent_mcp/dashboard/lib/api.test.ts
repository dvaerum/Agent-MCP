import { describe, it, expect } from "vitest"
import { buildTasksQuery, type TaskFilters } from "@/lib/api"

// Pure-Node assertions on the GET /tasks query-string builder. This is
// the single serialization point the Tasks page relies on to drive
// server-side filtering (status / assignment / creator), so the shape
// is a source property worth pinning: omit empties, dedicated
// assigned/unassigned booleans, AND-combination, back-compat empty.

describe("buildTasksQuery", () => {
  it("returns '' for no filters (back-compat GET /tasks)", () => {
    expect(buildTasksQuery()).toBe("")
    expect(buildTasksQuery(undefined)).toBe("")
  })

  it("returns '' for an empty / all-falsy filter object", () => {
    expect(buildTasksQuery({})).toBe("")
    expect(
      buildTasksQuery({
        status: "",
        assigned_to: "",
        created_by: "",
        assigned: false,
        unassigned: false,
      }),
    ).toBe("")
  })

  it("serializes a status filter (including the `incomplete` alias)", () => {
    expect(buildTasksQuery({ status: "incomplete" })).toBe("?status=incomplete")
    expect(buildTasksQuery({ status: "in_progress" })).toBe(
      "?status=in_progress",
    )
  })

  it("serializes assigned=true as the dedicated boolean", () => {
    expect(buildTasksQuery({ assigned: true })).toBe("?assigned=true")
  })

  it("serializes unassigned=true as the dedicated boolean", () => {
    expect(buildTasksQuery({ unassigned: true })).toBe("?unassigned=true")
  })

  it("omits the assignment booleans when false", () => {
    expect(buildTasksQuery({ assigned: false, unassigned: false })).toBe("")
  })

  it("serializes assigned_to and created_by agent ids", () => {
    expect(buildTasksQuery({ assigned_to: "agent-7" })).toBe(
      "?assigned_to=agent-7",
    )
    expect(buildTasksQuery({ created_by: "agent-3" })).toBe(
      "?created_by=agent-3",
    )
  })

  it("never emits a magic assigned_to=unassigned collision value", () => {
    // The claimable pool is expressed only via the `unassigned`
    // boolean — an agent literally named "unassigned" assigned to a
    // task must serialize as assigned_to, distinct from the pool.
    const qs = buildTasksQuery({ assigned_to: "unassigned" })
    expect(qs).toBe("?assigned_to=unassigned")
    expect(qs).not.toContain("unassigned=true")
  })

  it("AND-combines every dimension in one query string", () => {
    const filters: TaskFilters = {
      status: "incomplete",
      assigned_to: "agent-1",
      created_by: "agent-2",
      unassigned: true,
    }
    const params = new URLSearchParams(buildTasksQuery(filters).slice(1))
    expect(params.get("status")).toBe("incomplete")
    expect(params.get("assigned_to")).toBe("agent-1")
    expect(params.get("created_by")).toBe("agent-2")
    expect(params.get("unassigned")).toBe("true")
  })

  it("url-encodes filter values", () => {
    const qs = buildTasksQuery({ created_by: "[deleted-foo bar]" })
    // The raw brackets/space must be percent-encoded, not passed through.
    expect(qs).not.toContain(" ")
    expect(
      new URLSearchParams(qs.slice(1)).get("created_by"),
    ).toBe("[deleted-foo bar]")
  })
})
