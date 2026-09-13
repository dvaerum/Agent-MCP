/**
 * Regression guard: Prompt Book renders prompts that lack a `tags` key.
 *
 * On 2026-06-17 a Firefox-MCP click-through on the Prompt Book tab
 * surfaced `TypeError: s.tags is undefined` inside
 * `fetchPromptsCatalog`'s consumer code. Root cause: one of the 12
 * shipped prompts in `catalog.json` (`event-loop`) lacked the `tags`
 * key entirely while the dashboard read-sites dereferenced
 * `prompt.tags` directly.
 *
 * The catalog's canonical source is `rust/conexus-tools/prompts/
 * catalog.json` (Phase F, prancy-napping-pie: the Python copy at
 * `agent_mcp/prompts/catalog.json` this test originally read is
 * deleted) -- this test reads the Rust-side copy the dashboard's real
 * `GET /api/prompts/catalog` route actually serves.
 *
 * The fix is three layers of defense:
 *
 * 1. **Backfill** the missing `tags: []` in `catalog.json` (immediate).
 * 2. **Normalize at fetch** in `data-store.ts::fetchPromptsCatalog` so
 *    anything that flows through the zustand slice always carries a
 *    `tags` array even if the catalog drifts again.
 * 3. **Defensive `?? []`** on every read site in
 *    `prompt-book-dashboard.tsx` (belt + suspenders if the store is
 *    ever bypassed) and `prompt-book.ts::searchPrompts`.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const CATALOG = resolve(
  DASHBOARD_ROOT,
  "..",
  "..",
  "rust",
  "conexus-tools",
  "prompts",
  "catalog.json",
)

function readDashboardFile(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

interface CatalogPrompt {
  id?: string
  tags?: unknown
}

function loadCatalog(): { prompts: CatalogPrompt[] } {
  return JSON.parse(readFileSync(CATALOG, "utf8"))
}

describe("prompt book handles missing tags", () => {
  it("layer 1: every catalog.json prompt has a tags key", () => {
    const catalog = loadCatalog()
    const missing = catalog.prompts
      .filter((p) => !("tags" in p))
      .map((p) => p.id ?? "<no-id>")
    expect(
      missing,
      `rust/conexus-tools/prompts/catalog.json has prompts without a ` +
        `\`tags\` key: ${JSON.stringify(missing)}. Every entry must have ` +
        `"tags": [...] (possibly empty) so the dashboard can render ` +
        "without tripping `TypeError: s.tags is undefined`.",
    ).toEqual([])
  })

  it("layer 1: tags is always a JSON array", () => {
    const catalog = loadCatalog()
    const bad = catalog.prompts
      .filter((p) => !Array.isArray(p.tags))
      .map((p) => [p.id ?? "<no-id>", typeof p.tags])
    expect(
      bad,
      `rust/conexus-tools/prompts/catalog.json has prompts whose \`tags\` ` +
        `is not a JSON array: ${JSON.stringify(bad)}.`,
    ).toEqual([])
  })

  it("layer 2: data-store normalizes prompt tags at fetch", () => {
    // Normalize whitespace so the assertion isn't brittle to
    // formatting choices.
    const flat = readDashboardFile("lib/stores/data-store.ts").replace(/\s+/g, " ")
    expect(
      flat,
      "lib/stores/data-store.ts::fetchPromptsCatalog should map each " +
        "fetched prompt to `{ ...p, tags: p.tags ?? [] }` so downstream " +
        "consumers never see `undefined.tags`. This is the layer-2 " +
        "defense -- even if catalog.json drifts again, the store heals it.",
    ).toContain("tags: p.tags ?? []")
  })

  it("layer 3: prompt-book-dashboard uses defensive tags reads", () => {
    const src = readDashboardFile("components/dashboard/prompt-book-dashboard.tsx")
    // The unguarded patterns we are explicitly forbidding.
    const forbidden: [RegExp, string][] = [
      [/\bprompt\.tags\.slice\b/, "prompt.tags.slice"],
      [/\bprompt\.tags\.length\b/, "prompt.tags.length"],
      [/\bp\.tags\.some\b/, "p.tags.some"],
    ]
    for (const [pattern, label] of forbidden) {
      expect(
        pattern.test(src),
        `prompt-book-dashboard.tsx still has an unguarded dereference ` +
          `matching \`${label}\`. Wrap the access in ` +
          "`(prompt.tags ?? []).…` / `(p.tags ?? []).…` so a prompt " +
          "without `tags` doesn't throw `TypeError: s.tags is undefined`.",
      ).toBe(false)
    }
    // And the defensive form must actually be present (catches the
    // case where someone deletes the dereference outright and breaks
    // the UI a different way).
    expect(
      src,
      "prompt-book-dashboard.tsx should use `(prompt.tags ?? [])` at " +
        "the card-render sites.",
    ).toContain("(prompt.tags ?? [])")
    expect(
      src,
      "prompt-book-dashboard.tsx should use `(p.tags ?? [])` in the " +
        "search-filter site.",
    ).toContain("(p.tags ?? [])")
  })

  it("layer 3b: prompt-book search guards tags", () => {
    const src = readDashboardFile("lib/prompt-book.ts")
    expect(
      /\bprompt\.tags\.some\b/.test(src),
      "lib/prompt-book.ts::searchPrompts still dereferences " +
        "`prompt.tags.some` without a guard. Use " +
        "`(prompt.tags ?? []).some(…)` so a tags-less prompt doesn't throw.",
    ).toBe(false)
    expect(
      src,
      "lib/prompt-book.ts should use `(prompt.tags ?? []).some(…)` in " +
        "searchPrompts.",
    ).toContain("(prompt.tags ?? [])")
  })
})
