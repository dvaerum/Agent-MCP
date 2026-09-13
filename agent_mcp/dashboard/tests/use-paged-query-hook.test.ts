/**
 * Regression guards for the retirement of the ``usePagedQuery<T>`` hook.
 *
 * History
 * -------
 * PR 5 of the 2026-06-09 architecture review introduced ``usePagedQuery`` —
 * a single owner of the paginated-fetch state machine
 * (``{data, total, loading, error, refresh, lastFetch}``) that the tasks
 * and messages dashboards each used to hand-roll. The Wave 6 follow-up then
 * moved BOTH consumers onto the shared TanStack Query ``queryClient``:
 *
 * - F2 migrated ``tasks-dashboard.tsx`` onto ``useTasksQuery``
 *   (``lib/queries/tasks.ts``) — one query per ``['tasks', project,
 *   filters]``.
 * - F3 migrated ``messages-dashboard.tsx`` onto ``useMessagesQuery``
 *   (``lib/queries/messages.ts``) — one query per ``['messages', project,
 *   {filters, limit, offset}]``.
 *
 * With the last consumer gone, ``hooks/use-paged-query.ts`` had no importers
 * left, so F3 DELETED it. This guard is repointed — NOT weakened — from
 * "the hook exists and both pages use it" to "the hook is gone and both
 * pages ride TanStack Query". The single-source-of-truth property the hook
 * was created to deliver now lives in the shared ``queryClient``; a
 * reintroduction of the bespoke state machine would be the regression.
 *
 * These tests are text-parse regression guards (same convention as
 * ``test_dashboard_use_filters_hook.py``, now ported to this vitest
 * convention); the fork verifies behaviour via the vitest suites
 * (``tasks-query-*.test.*`` / ``messages-query-*.test.*``) plus
 * ``npm run build``.
 */

import { describe, expect, it } from "vitest"
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

function walkTsSources(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    if (name === "node_modules") continue
    const p = resolve(dir, name)
    if (statSync(p).isDirectory()) out.push(...walkTsSources(p))
    else if (/\.ts[x]?$/.test(name)) out.push(p)
  }
  return out
}

describe("usePagedQuery<T>() hook retirement", () => {
  // ---------- The hook is retired ----------------------------------

  it("has hooks/use-paged-query.ts GONE — its last consumer (messages-dashboard) migrated onto TanStack Query in F3, leaving no importers; a resurrected file means the bespoke paginated-fetch state machine crept back alongside the shared queryClient", () => {
    const path = resolve(DASHBOARD_ROOT, "hooks", "use-paged-query.ts")
    expect(
      existsSync(path),
      `expected ${path} to be deleted (all consumers migrated to ` +
        "TanStack Query); the hand-rolled paginated-fetch hook must not " +
        "be reintroduced",
    ).toBe(false)
  })

  it("has no source import the retired hook — a lingering import would fail the build (the file is gone); this catches it at the grep layer with a clearer message, and pins that neither a real import nor a vi.mock of the hook survives", () => {
    const offenders: string[] = []
    for (const path of walkTsSources(DASHBOARD_ROOT)) {
      const text = readFileSync(path, "utf8")
      // A real import / mock of the hook module — NOT the incidental
      // historical mentions in doc-comments (which describe lineage).
      if (
        /from\s+['"][^'"]*use-paged-query['"]/.test(text) ||
        /vi\.mock\(\s*['"][^'"]*use-paged-query['"]/.test(text)
      ) {
        offenders.push(path.slice(DASHBOARD_ROOT.length + 1))
      }
    }
    expect(
      offenders,
      "expected no module to import '@/hooks/use-paged-query' after " +
        `its removal; still referenced by: ${offenders}`,
    ).toEqual([])
  })

  // ---------- Consumer migrations (TanStack Query) -----------------

  it("has messages-dashboard.tsx migrated to TanStack Query — W6-followup F3: it no longer rides the hand-rolled usePagedQuery state machine; the list fetch moved onto the shared TanStack Query client via useMessagesQuery (see lib/queries/messages.ts), keyed ['messages', project, {filters, limit, offset}] with one SSE invalidation choke point. This guard was previously test_messages_dashboard_imports_use_paged_query (which asserted the OLD import). Repointed — NOT weakened — to the new location: the page must import the TanStack messages query and must NOT re-introduce the retired hook.", () => {
    const src = read("components/dashboard/messages-dashboard.tsx")
    expect(
      src.includes("useMessagesQuery"),
      "expected messages-dashboard.tsx to import useMessagesQuery (the " +
        "TanStack Query messages-list fetch)",
    ).toBe(true)
    expect(
      src.includes("lib/queries/messages"),
      "expected messages-dashboard.tsx to reference '@/lib/queries/messages'",
    ).toBe(true)
    expect(
      src.includes("usePagedQuery"),
      "expected messages-dashboard.tsx to NOT import usePagedQuery after " +
        "the F3 migration onto TanStack Query",
    ).toBe(false)
    expect(
      src.includes("use-paged-query"),
      "expected messages-dashboard.tsx to NOT reference " +
        "'@/hooks/use-paged-query' after the F3 migration",
    ).toBe(false)
  })

  it("no longer declares a messages useState in messages-dashboard.tsx — the legacy useState<Message[]>([]) (the rows-of-data slice) must be gone; the TanStack query owns the messages array now", () => {
    const src = read("components/dashboard/messages-dashboard.tsx")
    expect(
      /useState\s*<\s*Message\[\]\s*>/.test(src),
      "expected `useState<Message[]>([])` to be retired in favour " +
        "of useMessagesQuery owning the messages array",
    ).toBe(false)
  })

  it("retires the page poll and window listener in messages-dashboard.tsx — the pre-migration page ran its own 60s setInterval background poll and an mcp:resources-updated window listener to refetch the listing; both are replaced by the single invalidateMessages() SSE choke point (lib/mcp-notifications.ts); they must be gone from the page so there is ONE freshness path, not three", () => {
    const src = read("components/dashboard/messages-dashboard.tsx")
    // Match actual CODE usage (with the call paren) — a doc-comment that
    // names the retired mechanism to explain WHY it is gone is fine.
    expect(
      src.includes("setInterval("),
      "expected the 60s setInterval background poll to be retired in " +
        "favour of the SSE-driven invalidateMessages() refetch",
    ).toBe(false)
    expect(
      src.includes("addEventListener("),
      "expected the mcp:resources-updated window listener to be retired " +
        "in favour of the SSE-driven invalidateMessages() refetch",
    ).toBe(false)
  })

  it("has the messages list fetch live in the api layer — the listing POST to /messages/query moved out of the retired hook and into the api layer as getMessages, the same shape useTasksQuery gets from getTasks; pin that the api module owns the endpoint + the POST verb (the GET-with-body bug that birthed /messages/query must stay buried)", () => {
    const apiSrc = read("lib/api/messages.ts")
    expect(
      apiSrc.includes("getMessages"),
      "expected lib/api/messages.ts to export getMessages (the " +
        "paginated messages-list reader)",
    ).toBe(true)
    expect(
      apiSrc.includes("/messages/query"),
      "expected getMessages to POST to '/messages/query'",
    ).toBe(true)
    expect(
      /method\s*:\s*['"]POST['"]/.test(apiSrc),
      "expected getMessages to use method: 'POST' (browsers strip GET " +
        "bodies — the original bug)",
    ).toBe(true)
  })

  it("has tasks-dashboard.tsx migrated to TanStack Query — W6-followup F2 (kept green through F3): it rides the TanStack useTasksQuery and must NOT re-introduce the retired hook", () => {
    const src = read("components/dashboard/tasks-dashboard.tsx")
    expect(
      src.includes("useTasksQuery"),
      "expected tasks-dashboard.tsx to import useTasksQuery",
    ).toBe(true)
    expect(
      src.includes("lib/queries/tasks"),
      "expected tasks-dashboard.tsx to reference '@/lib/queries/tasks'",
    ).toBe(true)
    expect(
      src.includes("usePagedQuery"),
      "expected tasks-dashboard.tsx to NOT import usePagedQuery",
    ).toBe(false)
    expect(
      src.includes("use-paged-query"),
      "expected tasks-dashboard.tsx to NOT reference " +
        "'@/hooks/use-paged-query'",
    ).toBe(false)
  })
})
