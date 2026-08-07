/**
 * Wave 2 (cleanup-wave-2) regression guard.
 *
 * Wave 2 strips every frontend read of ``admin_token`` and migrates
 * the MCP notifications SSE client off bearer auth onto cookie auth.
 * These tests pin three properties that ``npm run build`` alone
 * cannot enforce — the build only checks TypeScript types, not the
 * runtime shape of the bytes we put on the wire.
 *
 * 1. ``DashboardData`` (the data-store slice) has NO ``admin_token``
 *    field. Pure type assertion via TS `// @ts-expect-error`.
 *
 * 2. No mutation call site in ``settings-dashboard.tsx`` /
 *    ``messages-dashboard.tsx`` / ``memories-dashboard.tsx`` writes
 *    a ``token:`` field into the request body. Grep-based because
 *    the assertion is on source bytes, not runtime behavior.
 *
 * 3. The MCP notifications client (``lib/mcp-notifications.ts``)
 *    uses ``credentials: "include"`` and never sets
 *    ``Authorization`` on its fetch. Stub fetch, drive the public
 *    entry point, inspect what arrived.
 *
 * If Wave 3 (drop ``admin_token`` from the backend response) lands
 * and the type now reflects that drop, assertion 1 must keep
 * passing — the field is simply gone, not surfaced.
 */

import { describe, expect, it, vi, afterEach } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
// Static import, not an in-test `await import()` — the module graph's
// transform+evaluation cost would otherwise be billed to `testTimeout`
// instead of the collection phase. See the note in
// `mcp-notifications-no-poll.test.ts` for the measurements.
import { openMcpNotificationStream } from "@/lib/mcp-notifications"

// Resolve relative to this test file rather than the cwd Vitest happens
// to launch from — the test runs identically from the repo root, from
// the dashboard dir, or from a CI subdir.
const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// ── 1. DashboardData has no admin_token field ─────────────────────

describe("Wave 2: DashboardData type", () => {
  it("does NOT carry an admin_token field on the AllData slice", async () => {
    // We can't import the type directly into runtime assertions
    // without a build step, so we read the source and check the
    // interface literally. The Wave 2 contract: the field is gone.
    // Wave 3 will remove it server-side; this test must keep
    // passing under both states.
    const src = read("lib/stores/data-store.ts")
    // Match a property declaration like `admin_token: string` or
    // `admin_token?: string` in the AllData interface body. Allow
    // doc-comment mentions and the file-level removal comment.
    const interfaceBody = src.match(
      /interface\s+AllData\s*{([\s\S]*?)}/m,
    )
    expect(interfaceBody, "AllData interface must be declared").not.toBeNull()
    const body = interfaceBody![1]
    // A property line is `admin_token` followed by optional `?` and a
    // `:`. Match it only as a property declaration (start-of-line or
    // semicolon boundary), so the "Wave 2 ... no longer surfaced"
    // comment in the interface body doesn't false-positive.
    const propMatch = body.match(/(^|;|\n)\s*admin_token\s*\??\s*:/)
    expect(
      propMatch,
      `AllData interface still declares an admin_token property:\n${body}`,
    ).toBeNull()
  })
})

// ── 2. No call site writes `token:` into a mutation body ───────────

describe("Wave 2: dashboard mutation call sites", () => {
  const FILES = [
    "components/dashboard/settings-dashboard.tsx",
    "components/dashboard/messages-dashboard.tsx",
    "components/dashboard/memories-dashboard.tsx",
  ]

  for (const file of FILES) {
    it(`${file} does not put a token field into any request body`, () => {
      const src = read(file)
      // The grep we want: any object-literal property whose KEY is
      // ``token`` (followed by colon + value), excluding the
      // ``getAgentToken`` selector signature and the recipient-
      // address strings. Match the literal ``token:`` at the start
      // of an object literal property, allowing leading whitespace,
      // comma, or opening-brace.
      //
      // Why this shape: a ``token:`` line inside a `body` object
      // sent to the backend would re-introduce the body-token auth
      // path the migration retired. We accept the cost of false-
      // positives by exempting the four well-known non-mutation
      // usages of the ``token:`` key (see EXEMPTIONS) rather than
      // trying to parse the source.
      const EXEMPTIONS: RegExp[] = [
        // Type signatures from the apiClient surface that aren't
        // mutation bodies — e.g. ``token: string`` on a method
        // parameter or response shape. None should remain after
        // Wave 2 in the listed files; this is purely defense in
        // depth if a future refactor folds an api signature here.
        /token\s*:\s*string\b/,
      ]
      const lines = src.split("\n")
      const offenders: string[] = []
      lines.forEach((line, idx) => {
        // Strip trailing/leading whitespace for the prefix check.
        const stripped = line.replace(/^\s+/, "")
        // Property-shape match: line starts with `token:` or
        // `token :`. Anything else (e.g. `getAgentToken(...)`,
        // `parent.token = ...`) doesn't begin with the property
        // form.
        if (!/^token\s*:/.test(stripped)) return
        // Skip lines that match an exemption.
        if (EXEMPTIONS.some((re) => re.test(stripped))) return
        offenders.push(`${file}:${idx + 1}  ${line.trim()}`)
      })
      expect(
        offenders,
        `Wave 2 violation: token-field writes still present:\n${offenders.join(
          "\n",
        )}`,
      ).toEqual([])
    })
  }
})

// ── 3. MCP notifications client uses cookie auth, no Authorization ─

describe("Wave 2: MCP notifications client", () => {
  const realFetch = globalThis.fetch
  afterEach(() => {
    globalThis.fetch = realFetch
  })

  it("opens the SSE stream with credentials:include and no Authorization", async () => {
    // Stub fetch globally so the module's stream-open call resolves
    // immediately. We don't need real SSE framing — we only care
    // about the request shape on the wire.
    const captured: { init?: RequestInit; url?: string }[] = []
    const fetchStub = vi.fn(async (url: string, init?: RequestInit) => {
      captured.push({ url, init })
      // Return a "stream" with one frame so the reader loop terminates.
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.close()
          },
        }),
        { status: 200 },
      )
    })
    // @ts-expect-error global stub for the duration of this test
    globalThis.fetch = fetchStub

    // Drive the lowest-level entry point so we don't depend on the
    // visibility-listener wiring in subscribeMcpNotifications. The
    // module reads `globalThis.fetch` at call time, so stubbing it
    // above (rather than before the import) is sufficient.
    const handle = openMcpNotificationStream({ url: "/mcp/test" })

    // Yield twice so the async fetch in the run loop kicks off.
    await new Promise((r) => setTimeout(r, 0))
    await new Promise((r) => setTimeout(r, 0))

    handle.stop()

    expect(captured.length).toBeGreaterThan(0)
    const { init } = captured[0]
    expect(init, "fetch was called without init").toBeTruthy()
    expect(init!.credentials, "must opt into cookie auth").toBe(
      "include",
    )
    const headers = (init!.headers ?? {}) as Record<string, string>
    // Headers may be a Headers instance or a plain object — both
    // should report no Authorization. Convert defensively.
    const hasAuth = (() => {
      if (typeof Headers !== "undefined" && headers instanceof Headers) {
        return headers.has("Authorization")
      }
      return Object.keys(headers).some(
        (k) => k.toLowerCase() === "authorization",
      )
    })()
    expect(
      hasAuth,
      "Wave 2 violation: Authorization header should be absent",
    ).toBe(false)
  })

  it("source no longer constructs an Authorization Bearer header", () => {
    // Belt-and-braces grep so a future refactor that swaps the
    // run-loop's fetch for an alternative transport can't silently
    // re-introduce the bearer header without flipping this assertion.
    const src = read("lib/mcp-notifications.ts")
    expect(
      /Authorization\s*:\s*[`'"]Bearer/.test(src),
      "lib/mcp-notifications.ts must not construct Authorization: Bearer",
    ).toBe(false)
  })
})
