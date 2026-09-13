/**
 * The dashboard API client MUST NOT retry mutating HTTP methods.
 *
 * The shared `request<T>()` helper in
 * `agent_mcp/dashboard/lib/api/client.ts` (the request core the
 * W6-followup F1 split extracted from the old `lib/api.ts` God-module)
 * implements a transparent retry-on-5xx loop intended to absorb the
 * ~10-15s cold-start latency of a lazily-spawned backend (router proxy
 * returns 502/503/504 while the UDS comes up). The retry was added in
 * the Candidate-C refactor (architecture review 2026-06-01) as a
 * universal wrapper — it does not branch on HTTP method.
 *
 * The bug: `request()` is reused for every API call, including
 * `createAgent` (POST /api/agents), `createTask` (POST /api/tasks),
 * `terminateAgent` (POST /api/terminate-agent), `editAgent`
 * (POST /api/agents/<id>/edit), `updateTask` (POST), and `purgeAgent`
 * (DELETE with body). When the backend processes a mutation, commits
 * the side-effect, and then crashes/disconnects returning 502 on the
 * response phase, the retry re-issues the same mutation. Two real
 * outcomes:
 *
 *   - createAgent → "agent_id already exists" 4xx (caught) on retry,
 *     but the first creation succeeded — the user sees an error and
 *     assumes nothing happened.
 *   - createTask → creates two tasks with identical title/description
 *     silently (task_id is server-generated so there's no uniqueness
 *     collision to catch the retry).
 *   - sendMessage / mark-as-read etc. — duplicate fan-out events.
 *
 * The fix: retries are safe ONLY for idempotent reads (GET / HEAD).
 * Mutations must surface the 5xx to the caller's catch handler so the
 * operator can see what happened and decide whether to retry manually.
 *
 * This test pins the contract by source-grep on the request core (house
 * source-grep convention, same pattern as the no-auto-cleanup and
 * no-legacy-redirects guards).
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const API_FILE = resolve(__dirname, "..", "lib", "api", "client.ts")

describe("dashboard API client: no retry on mutating methods", () => {
  it("API client file exists", () => {
    // Sanity: a rename of client.ts invalidates the other assertions.
    expect(
      () => readFileSync(API_FILE, "utf8"),
      `Expected dashboard API client at ${API_FILE}; either it was ` +
        "moved/renamed (update this test) or the repo layout changed.",
    ).not.toThrow()
  })

  it("gates the retry loop on the request method", () => {
    // The retry loop in `request<T>()` must check the request method
    // before deciding to retry on 5xx.
    //
    // We look for the `for (let attempt = 0; attempt < 3; attempt++)`
    // loop body (or its successor — the cap of 3 may change) and assert
    // the body references `method` somehow. A naive future contributor
    // who deletes the method check (returning the universal-retry bug)
    // will trip this test.
    //
    // Specifically, the retry-eligibility condition should reference
    // either the literal string `'GET'` (the safe-method allowlist) or
    // `method` (referencing the request method variable). The original
    // buggy implementation checked only `response.status`, no method
    // involvement at all.
    const source = readFileSync(API_FILE, "utf8")

    // Locate the request<T>() method body. Anchor on the
    // `async request<T>(` signature (the W6-followup F1 split made it a
    // public method on the extracted request core, so the leading
    // `private` is now optional); grab until the matching outer `}` at
    // two-space indent (the class member end).
    const fnMatch = source.match(
      /(?:private\s+)?async\s+request<T>\([^)]*\)[^{]*\{([\s\S]*?)\n\s{2}\}/,
    )
    expect(
      fnMatch,
      "Couldn't locate the `request<T>()` method body in client.ts; " +
        "it may have been renamed. Update this test.",
    ).not.toBeNull()
    const body = fnMatch![1]!

    // The retry loop must exist (we don't want someone to "fix" the bug
    // by deleting the whole retry — the cold-start absorption is still
    // useful for GETs).
    expect(
      body.includes("attempt"),
      "`request<T>()` no longer has an `attempt` retry loop. The " +
        "transparent cold-start retry is intentional for GET — removing " +
        "it would resurface the boundary-level useEffect retry loop the " +
        "Candidate-C refactor replaced. If the loop was renamed, update " +
        "this test; if it was deleted, restore it but keep the method gate.",
    ).toBe(true)

    // The retry-eligibility check must reference the method somehow.
    // We accept either:
    //   (a) the literal 'GET' or "GET" string appearing in the function
    //       body (idempotent-method allowlist), or
    //   (b) a `method` identifier appearing in the body, used in the
    //       retry-eligibility branch.
    // Either way the body must NOT decide retry-eligibility purely from
    // `response.status` without any method involvement (the buggy
    // original).
    const hasGetLiteral = body.includes("'GET'") || body.includes('"GET"')
    const hasMethodRef = /\bmethod\b/.test(body)
    expect(
      hasGetLiteral || hasMethodRef,
      "The `request<T>()` retry loop in client.ts does not reference " +
        "the HTTP method anywhere. Universal-retry-on-5xx double-fires " +
        "mutations (POST createAgent, createTask, terminateAgent etc.) " +
        "when the backend processes the mutation and then returns 502 on " +
        "the response phase. Retries must be gated to idempotent methods " +
        "(GET / HEAD). See the docstring for the bug shape.",
    ).toBe(true)
  })
})
