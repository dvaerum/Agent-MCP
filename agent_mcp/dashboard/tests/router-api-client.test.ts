/**
 * RouterApiClient (lib/router-api.ts) regression guard.
 *
 * The router-admin surface (/agent-mcp/api/router/*) used to be reached
 * by ~10 components hand-rolling ``fetch`` — each re-typing the strict
 * Accept media type + ``credentials:'include'`` and, critically, none
 * of them bouncing a 401 to the login page (they surfaced an opaque
 * "HTTP 401"). This test pins the three properties the consolidated
 * client now owns for ALL of those call sites:
 *
 *   1. Every request sends the strict ``application/vnd.agent-mcp.v1+json``
 *      Accept header and ``credentials: 'include'`` (the operator
 *      session cookie).
 *   2. A 401 redirects the browser to /agent-mcp/login with the current
 *      path preserved in ?next= (the bug the raw-fetch sites all had).
 *   3. An !ok response throws a typed ``ApiError`` whose message prefers
 *      the server's ``{message}`` field over the status line.
 *
 * Env is node (no jsdom), so ``window`` is stubbed explicitly for the
 * redirect assertion — same as the real ``typeof window`` guard sees in
 * the browser.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"
import { ApiError } from "@/lib/api"
import { request } from "@/lib/router-api"

const ACCEPT = "application/vnd.agent-mcp.v1+json"

function headerValue(init: RequestInit | undefined, name: string): string | null {
  const headers = (init?.headers ?? {}) as Record<string, string>
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    return headers.get(name)
  }
  const key = Object.keys(headers).find(
    (k) => k.toLowerCase() === name.toLowerCase(),
  )
  return key ? headers[key] ?? null : null
}

describe("routerApi.request", () => {
  const realWindow = (globalThis as { window?: unknown }).window
  const realFetch = globalThis.fetch

  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    // Restore any window/fetch we stubbed so tests stay isolated.
    ;(globalThis as { window?: unknown }).window = realWindow
    globalThis.fetch = realFetch
  })

  it("sends the strict Accept header and credentials:include", async () => {
    const captured: { url?: string; init?: RequestInit }[] = []
    globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      captured.push({ url, init })
      return new Response(JSON.stringify({ ok: true }), { status: 200 })
    }) as unknown as typeof fetch

    const body = await request<{ ok: boolean }>("/agent-mcp/api/router/users")

    expect(body).toEqual({ ok: true })
    expect(captured.length).toBe(1)
    expect(captured[0]!.url).toBe("/agent-mcp/api/router/users")
    expect(headerValue(captured[0]!.init, "Accept")).toBe(ACCEPT)
    expect(captured[0]!.init?.credentials).toBe("include")
  })

  it("redirects to the login page on a 401 and preserves ?next=", async () => {
    const assign = vi.fn()
    ;(globalThis as { window?: unknown }).window = {
      location: {
        pathname: "/agent-mcp/app/washing-brothers",
        search: "?tab=users",
        assign,
      },
    }
    globalThis.fetch = vi.fn(async () => {
      return new Response(JSON.stringify({ message: "no cookie" }), {
        status: 401,
      })
    }) as unknown as typeof fetch

    await expect(
      request("/agent-mcp/api/router/users"),
    ).rejects.toBeInstanceOf(ApiError)

    expect(assign).toHaveBeenCalledTimes(1)
    const target = assign.mock.calls[0]![0] as string
    expect(target).toContain("/agent-mcp/login")
    expect(target).toContain(
      `next=${encodeURIComponent("/agent-mcp/app/washing-brothers?tab=users")}`,
    )
  })

  it("throws an ApiError that prefers the server {message} over the status line", async () => {
    ;(globalThis as { window?: unknown }).window = undefined
    globalThis.fetch = vi.fn(async () => {
      return new Response(
        JSON.stringify({ success: false, error: "bad", message: "username taken" }),
        { status: 400 },
      )
    }) as unknown as typeof fetch

    const err = await request("/agent-mcp/api/router/users", {
      method: "POST",
      body: JSON.stringify({ username: "x" }),
    }).catch((e) => e)

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(400)
    expect((err as ApiError).message).toBe("username taken")
  })
})
