import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import {
  apiClient,
  ApiError,
  ShapeError,
  allDataGuard,
  buildTasksQuery,
  normalizeTask,
  normalizeTaskListField,
  type RawTask,
  type TaskFilters,
} from "@/lib/api"

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

// Engine-level coverage for ApiClient.request() — the single fetch
// funnel every endpoint method flows through. request() itself is
// private, so these tests drive it through public methods
// (getSystemStatus = GET, createTask = POST) with a `fetch` stub,
// asserting the four behaviours that are easy to regress:
//   1. success JSON is parsed and returned as-is,
//   2. a non-OK response becomes an ApiError with the {status, message,
//      body} shape and the message-preference cascade,
//   3. a 401 surfaces as ApiError(401) (the redirect is window-guarded;
//      the vitest env is `node`, so window is undefined and request()
//      falls through to the generic ApiError path),
//   4. the 5xx retry is gated on the HTTP method — GET retries, POST
//      does not (retrying a mutation double-fires side-effects).

function fakeResponse(status: number, body: unknown, ok?: boolean): Response {
  const text = typeof body === "string" ? body : JSON.stringify(body)
  return {
    ok: ok ?? (status >= 200 && status < 300),
    status,
    statusText: `Status ${status}`,
    text: async () => text,
    json: async () => (typeof body === "string" ? JSON.parse(body || "null") : body),
  } as unknown as Response
}

async function catchApiError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (e) {
    return e as ApiError
  }
  throw new Error("expected the request to reject, but it resolved")
}

describe("ApiClient.request engine", () => {
  const realFetch = global.fetch

  beforeEach(() => {
    apiClient.setBaseUrl("/api")
  })

  afterEach(() => {
    global.fetch = realFetch
    vi.restoreAllMocks()
  })

  it("parses and returns a successful JSON body", async () => {
    global.fetch = vi.fn(
      async () => fakeResponse(200, { server_running: true }),
    ) as unknown as typeof fetch

    const res = await apiClient.getSystemStatus()
    expect(res).toEqual({ server_running: true })
  })

  it("throws an ApiError carrying {status, message, body} on a non-OK response", async () => {
    const payload = { message: "the thing broke" }
    global.fetch = vi.fn(
      async () => fakeResponse(400, payload),
    ) as unknown as typeof fetch

    const err = await catchApiError(apiClient.getSystemStatus())
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(400)
    expect(err.message).toBe("the thing broke")
    expect(err.body).toBe(JSON.stringify(payload))
  })

  it("prefers message > detail > error > raw body > status line", async () => {
    const cases: Array<{ body: unknown; expected: string }> = [
      { body: { message: "m", detail: "d", error: "e" }, expected: "m" },
      { body: { detail: "d", error: "e" }, expected: "d" },
      { body: { error: "e" }, expected: "e" },
      { body: "plain-text failure", expected: "plain-text failure" },
    ]
    for (const { body, expected } of cases) {
      global.fetch = vi.fn(
        async () => fakeResponse(422, body),
      ) as unknown as typeof fetch
      const err = await catchApiError(apiClient.getSystemStatus())
      expect(err.message).toBe(expected)
    }

    // Empty body → nothing to surface, so fall back to the status line.
    global.fetch = vi.fn(
      async () => fakeResponse(500, ""),
    ) as unknown as typeof fetch
    const err = await catchApiError(apiClient.getSystemStatus())
    expect(err.message).toContain("500")
  })

  it("surfaces a 401 as ApiError(401)", async () => {
    global.fetch = vi.fn(
      async () => fakeResponse(401, { message: "session expired" }),
    ) as unknown as typeof fetch

    const err = await catchApiError(apiClient.getSystemStatus())
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(401)
  })

  it("retries a read-only (GET) request on a 5xx then returns the recovered body", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(fakeResponse(503, { message: "cold start" }))
      .mockResolvedValueOnce(fakeResponse(200, { server_running: true }))
    global.fetch = fetchMock as unknown as typeof fetch

    const res = await apiClient.getSystemStatus()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(res).toEqual({ server_running: true })
  })

  it("does NOT retry a mutating (POST) request on a 5xx", async () => {
    const fetchMock = vi.fn(async () => fakeResponse(502, { message: "gateway" }))
    global.fetch = fetchMock as unknown as typeof fetch

    const err = await catchApiError(apiClient.createTask({ title: "x" }))
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(502)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

// TY-1: the runtime shape guard at the request<T>() boundary. A 200 OK
// whose JSON is structurally wrong must throw a ShapeError HERE (at the
// seam, naming the endpoint + mismatch) rather than be trusted with a
// bare `as T` cast and blow up deep in a store consumer.
describe("ApiClient.request shape validation (TY-1)", () => {
  const realFetch = global.fetch

  beforeEach(() => {
    apiClient.setBaseUrl("/api")
  })

  afterEach(() => {
    global.fetch = realFetch
    vi.restoreAllMocks()
  })

  it("rejects a 200 whose /status body is not a SystemStatus", async () => {
    // Backend returns 200 but the body is an array, not the object with
    // a boolean server_running the caller was promised.
    global.fetch = vi.fn(
      async () => fakeResponse(200, [1, 2, 3]),
    ) as unknown as typeof fetch

    const err = await catchApiError(apiClient.getSystemStatus())
    expect(err).toBeInstanceOf(ShapeError)
    expect(err.message).toContain("/status")
  })

  it("rejects a 200 whose /tasks array member lacks task_id", async () => {
    global.fetch = vi.fn(
      async () => fakeResponse(200, [{ title: "no id" }]),
    ) as unknown as typeof fetch

    const err = await catchApiError(apiClient.getTasks())
    expect(err).toBeInstanceOf(ShapeError)
  })

  it("accepts and returns a well-shaped body unchanged", async () => {
    global.fetch = vi.fn(
      async () => fakeResponse(200, { server_running: true, total_agents: 2 }),
    ) as unknown as typeof fetch

    const res = await apiClient.getSystemStatus()
    expect(res.server_running).toBe(true)
  })
})

// AUDIT AF-A / TY-1: the `/all-data` bulk envelope is the highest-traffic
// read path (feeds agents/tasks/context to every page) yet used to skip
// the request<T>() shape guard. A structurally-wrong 200 (agents/tasks/
// context missing or renamed) must throw a ShapeError at the seam — naming
// the endpoint — instead of a bare cast that blows up in a consumer's
// `.find`/`.map` far away.
describe("allDataGuard (TY-1)", () => {
  const wellShaped = {
    agents: [],
    tasks: [],
    context: [],
    actions: [],
    file_metadata: [],
    file_map: {},
    timestamp: "2026-01-01",
  }

  it("rejects a non-object envelope, naming the endpoint", () => {
    expect(() => allDataGuard([1, 2, 3])).toThrow(ShapeError)
    expect(() => allDataGuard(null)).toThrow(/\/all-data/)
  })

  it("rejects an envelope whose agents field is missing/renamed", () => {
    // 200 OK, but `agents` absent → the exact break selectAgent hits.
    const renamed = { ...wellShaped, agents: undefined }
    expect(() => allDataGuard(renamed)).toThrow(ShapeError)
    expect(() => allDataGuard(renamed)).toThrow(/agents/)
  })

  it("rejects an envelope whose tasks/context are not arrays", () => {
    expect(() => allDataGuard({ ...wellShaped, tasks: "nope" })).toThrow(
      /tasks/,
    )
    expect(() => allDataGuard({ ...wellShaped, context: {} })).toThrow(
      /context/,
    )
  })

  it("accepts a well-shaped envelope unchanged", () => {
    expect(allDataGuard(wellShaped)).toBe(wellShaped)
  })
})

describe("getAllData shape validation (TY-1, end-to-end)", () => {
  const realFetch = global.fetch

  beforeEach(() => {
    apiClient.setBaseUrl("/api")
  })

  afterEach(() => {
    global.fetch = realFetch
    vi.restoreAllMocks()
  })

  it("rejects a 200 /all-data whose agents field is missing", async () => {
    global.fetch = vi.fn(
      async () =>
        fakeResponse(200, {
          // agents omitted — the malformed envelope the audit flagged.
          tasks: [],
          context: [],
          actions: [],
          file_metadata: [],
          file_map: {},
          timestamp: "x",
        }),
    ) as unknown as typeof fetch

    const err = await catchApiError(apiClient.getAllData())
    expect(err).toBeInstanceOf(ShapeError)
    expect(err.message).toContain("/all-data")
  })

  it("normalizes raw task rows on a well-shaped envelope", async () => {
    global.fetch = vi.fn(
      async () =>
        fakeResponse(200, {
          agents: [],
          tasks: [
            {
              task_id: "t1",
              title: "T",
              status: "pending",
              priority: "medium",
              created_at: "x",
              updated_at: "x",
              child_tasks: '["c1"]',
              depends_on_tasks: null,
            },
          ],
          context: [],
          actions: [],
          file_metadata: [],
          file_map: {},
          timestamp: "x",
        }),
    ) as unknown as typeof fetch

    const env = await apiClient.getAllData()
    expect(env.tasks[0]!.child_tasks).toEqual(["c1"])
    expect(env.tasks[0]!.depends_on_tasks).toEqual([])
  })
})

// TY-2: child_tasks / depends_on_tasks are polymorphic on the wire
// (JSON string | array | null). The lib boundary normalizes both to
// string[] so every consumer sees exactly one shape.
describe("normalizeTaskListField (TY-2)", () => {
  it("parses a JSON-array string", () => {
    expect(normalizeTaskListField('["a","b"]')).toEqual(["a", "b"])
  })

  it("passes an array through, keeping only string members", () => {
    expect(normalizeTaskListField(["a", "b"])).toEqual(["a", "b"])
    expect(normalizeTaskListField(["a", 1, null, "b"] as unknown)).toEqual([
      "a",
      "b",
    ])
  })

  it("returns [] for null / undefined / empty / non-JSON / non-array-JSON", () => {
    expect(normalizeTaskListField(null)).toEqual([])
    expect(normalizeTaskListField(undefined)).toEqual([])
    expect(normalizeTaskListField("")).toEqual([])
    expect(normalizeTaskListField("task_abc")).toEqual([])
    expect(normalizeTaskListField('{"a":1}')).toEqual([])
  })
})

describe("normalizeTask (TY-2)", () => {
  const base: RawTask = {
    task_id: "t1",
    title: "T",
    status: "pending",
    priority: "medium",
    created_at: "2026-01-01",
    updated_at: "2026-01-01",
  } as RawTask

  it("collapses JSON-string list fields to arrays", () => {
    const out = normalizeTask({
      ...base,
      child_tasks: '["c1","c2"]',
      depends_on_tasks: '["d1"]',
    })
    expect(out.child_tasks).toEqual(["c1", "c2"])
    expect(out.depends_on_tasks).toEqual(["d1"])
  })

  it("is idempotent — arrays pass through", () => {
    const out = normalizeTask({
      ...base,
      child_tasks: ["c1"],
      depends_on_tasks: ["d1"],
    })
    expect(out.child_tasks).toEqual(["c1"])
    expect(out.depends_on_tasks).toEqual(["d1"])
  })

  it("defaults missing/null list fields to []", () => {
    const out = normalizeTask({ ...base, child_tasks: null })
    expect(out.child_tasks).toEqual([])
    expect(out.depends_on_tasks).toEqual([])
  })
})

// TY-2 end-to-end through the request boundary: getTasks() hands back
// Tasks whose child_tasks is already an array, even though the wire
// sent a JSON string.
describe("getTasks normalization (TY-2, end-to-end)", () => {
  const realFetch = global.fetch

  beforeEach(() => {
    apiClient.setBaseUrl("/api")
  })

  afterEach(() => {
    global.fetch = realFetch
    vi.restoreAllMocks()
  })

  it("normalizes wire JSON-string child_tasks into an array", async () => {
    global.fetch = vi.fn(
      async () =>
        fakeResponse(200, [
          {
            task_id: "t1",
            title: "T",
            status: "pending",
            priority: "medium",
            created_at: "x",
            updated_at: "x",
            child_tasks: '["c1","c2"]',
            depends_on_tasks: null,
          },
        ]),
    ) as unknown as typeof fetch

    const [task] = await apiClient.getTasks()
    expect(task.child_tasks).toEqual(["c1", "c2"])
    expect(task.depends_on_tasks).toEqual([])
  })
})
