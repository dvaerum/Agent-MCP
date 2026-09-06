// @vitest-environment jsdom
//
// Firefox-MCP finding (2026-09-06): `ApiClient.request()`'s 401
// handling was unconditional — ANY 401 on the per-project REST surface
// forced an immediate hard redirect to /agent-mcp/login, discarding
// the current page/state. That's correct for a genuinely expired
// session, but the router's `/agent-mcp/api/<project>/*` proxy route
// is NOT behind the operator session-gate (it forwards straight to
// the per-project backend's own REST gate), and that backend gate
// returns the SAME `{"error":"login_required"}` envelope whether the
// session cookie is genuinely gone or the router simply failed to
// bridge a perfectly valid cookie into the header shape the backend
// expects — a router-side bug, not a session problem. The two cases
// are indistinguishable at the HTTP layer (see `client.ts`'s retry-
// loop comment), so the defensible fix is a bounded retry (riding the
// SAME cold-start backoff the 5xx path already uses) before concluding
// "this really is an expired session" — never an unconditional bounce
// on the very first 401.
//
// Needs jsdom (not the suite's default `node` env): the redirect path
// this pins reads `window.location` and calls `window.location.assign`,
// which only exists with a DOM. `lib/api.test.ts`'s existing 401
// coverage runs in `node`, where `window` is undefined and the
// redirect branch is structurally skipped — it can't exercise this
// behaviour at all.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { apiClient, ApiError } from "@/lib/api"
import { loginUrl } from "@/lib/urls"

function fakeResponse(status: number, body: unknown): Response {
  const text = JSON.stringify(body)
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `Status ${status}`,
    text: async () => text,
    json: async () => body,
  } as unknown as Response
}

let locationAssign: ReturnType<typeof vi.fn>

beforeEach(() => {
  apiClient.setBaseUrl("/api")
  locationAssign = vi.fn()
  // jsdom's real navigation isn't implemented; stub `assign` so a
  // forced-logout doesn't spam "Not implemented: navigation" and so
  // we can assert on it. Same pattern as
  // components/layout/header.test.tsx.
  vi.stubGlobal("location", { ...window.location, assign: locationAssign })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const loginRequiredBody = { error: "login_required", message: "no cookie" }

describe("ApiClient.request 401 handling — bounded retry before forced logout", () => {
  it("does NOT force a logout on a single transient 401 that recovers on retry (GET)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(fakeResponse(401, loginRequiredBody))
      .mockResolvedValueOnce(fakeResponse(200, { server_running: true }))
    vi.stubGlobal("fetch", fetchMock)

    const res = await apiClient.getSystemStatus()

    expect(res).toEqual({ server_running: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(locationAssign).not.toHaveBeenCalled()
  })

  it("still forces a real logout when the 401 persists across every retry (GET)", async () => {
    const fetchMock = vi.fn(async () => fakeResponse(401, loginRequiredBody))
    vi.stubGlobal("fetch", fetchMock)

    await expect(apiClient.getSystemStatus()).rejects.toBeInstanceOf(ApiError)

    // 3 attempts total: the same bound the 5xx cold-start retry uses.
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(locationAssign).toHaveBeenCalledTimes(1)
    expect(locationAssign.mock.calls[0]![0] as string).toContain(loginUrl())
  })

  it("does NOT retry a mutating (POST) request on a 401 — forces logout immediately", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(fakeResponse(401, loginRequiredBody))
      .mockResolvedValueOnce(fakeResponse(200, { id: "task-1" }))
    vi.stubGlobal("fetch", fetchMock)

    await expect(
      apiClient.createTask({ title: "x" }),
    ).rejects.toBeInstanceOf(ApiError)

    // No retry for a mutation: a real 401 on a write must surface
    // immediately, not risk a silently-retried side effect.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(locationAssign).toHaveBeenCalledTimes(1)
  })
})
