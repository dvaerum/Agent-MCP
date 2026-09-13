/**
 * Regression guard: dashboard fetches prompt catalog from the REST API.
 *
 * The dashboard used to inline the prompt catalog as a 470-line
 * TypeScript array in `lib/prompt-book.ts`. PR #67 added the JSON
 * source of truth at `agent_mcp/prompts/catalog.json` and exposed it
 * via `GET /api/prompts/catalog`; an interim drift-detection test
 * (`test_typescript_and_json_catalogs_in_sync`) ensured the two
 * catalogues stayed aligned until this migration happened.
 *
 * This test pins the post-migration shape:
 *
 * - `prompt-book.ts` no longer carries an inlined `export const
 *   promptTemplates` array. The data comes from the REST endpoint via
 *   the zustand `useDataStore` slice instead.
 *
 * - `data-store.ts` carries a `promptsCatalog` state field plus a
 *   `fetchPromptsCatalog` action so the rest of the dashboard reads
 *   the catalogue the same way it reads agents / tasks / context.
 *
 * - `prompt-book-dashboard.tsx` no longer imports `promptTemplates`
 *   directly — it pulls the catalogue from the store.
 *
 * - The notification listener (`lib/api.ts` or a sibling file) calls
 *   `invalidatePromptsCatalog` when an MCP `notifications/prompts/list_changed`
 *   arrives so other dashboard tabs see an admin-created custom prompt
 *   within seconds rather than on the next manual reload.
 *
 * Ported from tests/test_dashboard_prompts_from_rest.py (Python source
 * tree retired) — this is a pure source-grep regression guard, same
 * convention as the rest of this suite: no dashboard runtime needed.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

describe("prompt-book.ts no longer inlines promptTemplates", () => {
  it("`lib/prompt-book.ts` must not declare `export const promptTemplates` as a literal array", () => {
    const src = read("lib/prompt-book.ts")
    const inlined = /export\s+const\s+promptTemplates\s*:\s*PromptTemplate\[\]\s*=\s*\[/.exec(
      src,
    )
    expect(
      inlined,
      "lib/prompt-book.ts still has an inlined `export const " +
        "promptTemplates: PromptTemplate[] = [...]` literal — the migration " +
        "to fetch from /api/prompts/catalog should have removed it. " +
        "Replace consumers with `useDataStore(s => s.promptsCatalog)`.",
    ).toBeNull()
  })
})

describe("data-store.ts exposes the promptsCatalog slice", () => {
  it("declares promptsCatalog, fetchPromptsCatalog, and invalidatePromptsCatalog", () => {
    const src = read("lib/stores/data-store.ts")
    expect(
      /\bpromptsCatalog\b/.test(src),
      "data-store.ts has no `promptsCatalog` field — add the slice " +
        "alongside the existing agents/tasks slices.",
    ).toBe(true)
    expect(
      /\bfetchPromptsCatalog\b/.test(src),
      "data-store.ts has no `fetchPromptsCatalog` action — add an " +
        "action that calls apiClient.getPromptsCatalog() and populates " +
        "the slice.",
    ).toBe(true)
    expect(
      /\binvalidatePromptsCatalog\b/.test(src),
      "data-store.ts has no `invalidatePromptsCatalog` action — the " +
        "notification listener needs a way to force-refresh.",
    ).toBe(true)
  })
})

describe("prompt-book-dashboard.tsx reads from the store", () => {
  it("uses useDataStore and does not import the legacy promptTemplates value", () => {
    const src = read("components/dashboard/prompt-book-dashboard.tsx")
    expect(
      src.includes("useDataStore"),
      "prompt-book-dashboard.tsx must read promptsCatalog from " +
        "useDataStore (the zustand slice) — current imports still point " +
        "at the inlined `promptTemplates` array.",
    ).toBe(true)
    // The legacy named import of `promptTemplates` from `@/lib/prompt-book`
    // must be gone. (A named import of `PromptTemplate` — the *type* — is
    // fine; we're guarding against the *value* import.)
    const badImports =
      /import\s*\{[^}]*\bpromptTemplates\b[^}]*\}\s*from\s*['"]@\/lib\/prompt-book['"]/.exec(
        src,
      )
    expect(
      badImports,
      "prompt-book-dashboard.tsx still imports `promptTemplates` from " +
        "@/lib/prompt-book — pull from `useDataStore(s => s.promptsCatalog)` " +
        "instead.",
    ).toBeNull()
  })
})

describe("api client exposes getPromptsCatalog", () => {
  it("`lib/api/system.ts` declares getPromptsCatalog()", () => {
    const src = read("lib/api/system.ts")
    expect(
      /\bgetPromptsCatalog\b/.test(src),
      "lib/api/system.ts has no getPromptsCatalog method — add it " +
        "alongside getAllData / getTokens etc.",
    ).toBe(true)
  })
})

describe("notification listener invalidates the prompts catalog", () => {
  it("wires notifications/prompts/list_changed to invalidatePromptsCatalog", () => {
    const combined = [
      read("lib/mcp-notifications.ts"),
      read("lib/stores/data-store.ts"),
    ].join("\n")
    expect(
      combined.includes("prompts/list_changed") ||
        combined.includes("promptsListChanged"),
      "No reference to MCP `notifications/prompts/list_changed` " +
        "found in lib/mcp-notifications.ts or lib/stores/data-store.ts — wire a " +
        "listener that calls invalidatePromptsCatalog so dashboards " +
        "in other tabs see admin-created prompts in real-time.",
    ).toBe(true)
    expect(
      combined.includes("invalidatePromptsCatalog"),
      "Listener doesn't call invalidatePromptsCatalog after a " +
        "prompts/list_changed notification — without invalidation, " +
        "subsequent reads return the stale cached value.",
    ).toBe(true)
  })
})
