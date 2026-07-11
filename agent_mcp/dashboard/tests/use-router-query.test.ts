/**
 * useRouterQuery (hooks/use-router-query.ts) — the 403-fold guard.
 *
 * The hook itself is a React hook and can't be rendered here (this
 * project's Vitest harness runs in the plain "node" environment with
 * no jsdom / React-testing-library — see vitest.config.ts). Its core
 * state-transition logic is factored out as ``resolveRouterQuery``, a
 * plain async function with no React dependency, precisely so this
 * invariant is testable the same way ``router-api-client.test.ts``
 * tests ``request()``: a pure async call against a stubbed fetcher.
 *
 * This test pins the three outcomes every router-admin component
 * used to hand-roll independently:
 *   1. A 403 (``ApiError``) → ``{kind: "forbidden"}`` — no error text.
 *   2. A 500 (``ApiError``) → ``{kind: "error"}`` — not forbidden.
 *   3. A successful fetch → ``{kind: "success", data}``.
 */

import { describe, expect, it } from "vitest"
import { ApiError } from "@/lib/api"
import { resolveRouterQuery } from "@/hooks/use-router-query"

function noopSignal(): AbortSignal {
  return new AbortController().signal
}

describe("resolveRouterQuery", () => {
  it("folds a 403 ApiError into forbidden, with no error text", async () => {
    const outcome = await resolveRouterQuery(async () => {
      throw new ApiError(403, "requires sysadmin", "")
    }, noopSignal())

    expect(outcome).toEqual({ kind: "forbidden" })
  })

  it("surfaces a 500 ApiError as error, not forbidden", async () => {
    const outcome = await resolveRouterQuery(async () => {
      throw new ApiError(500, "internal error", "")
    }, noopSignal())

    expect(outcome.kind).toBe("error")
    if (outcome.kind === "error") {
      expect(outcome.error.message).toBe("internal error")
    }
  })

  it("resolves data on success", async () => {
    const outcome = await resolveRouterQuery(
      async () => ({ mode: "builtin" }),
      noopSignal(),
    )

    expect(outcome).toEqual({ kind: "success", data: { mode: "builtin" } })
  })

  it("treats a non-Error, non-ApiError throw as a plain error", async () => {
    const outcome = await resolveRouterQuery(async () => {
      throw "raw string failure"
    }, noopSignal())

    expect(outcome.kind).toBe("error")
    if (outcome.kind === "error") {
      expect(outcome.error.message).toBe("raw string failure")
    }
  })

  it("reports aborted when the signal is already aborted before the fetch resolves", async () => {
    const controller = new AbortController()
    controller.abort()

    const outcome = await resolveRouterQuery(async (signal) => {
      // Simulate a fetch that respects the abort signal.
      expect(signal.aborted).toBe(true)
      throw new DOMException("The operation was aborted.", "AbortError")
    }, controller.signal)

    expect(outcome).toEqual({ kind: "aborted" })
  })
})
