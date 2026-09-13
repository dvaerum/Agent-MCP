/**
 * Regression guards for the destructive-action confirmation tiers.
 *
 * The model (canonical write-up lives in
 * `agent_mcp/dashboard/components/dashboard/modals/confirm-action-modal.tsx`
 * — do not restate it here, reference it):
 *
 *   * tier 0 — no dialog, success toast
 *   * tier 1 — `<ConfirmActionModal>`: simple confirm that NAMES the target
 *   * tier 2 — `<DeleteConfirmModal>`: type `DELETE`
 *   * tier 3 — `<DeleteConfirmModal requiredWord={name} matchCase>`: type
 *     the entity's own name/id, case-sensitively
 *
 * Two properties are worth pinning in text, because both are the kind
 * of thing a later "let's make this consistent" refactor silently
 * undoes:
 *
 * 1. **Polymorphism.** Users type the username, groups the group name,
 *    projects the project name, purge the agent id. Four different
 *    strings means the gesture cannot become a reflex. A uniform
 *    `DELETE` across those pages would be ONE muscle-memory sequence
 *    that opens every tier-3 gate in the product.
 * 2. **Tier 1 stays cheap.** Memories / Schedules / Terminate / leaf-task
 *    delete must NOT carry a type-to-confirm gate. Habituation is what
 *    makes the expensive gates worthless, so the cheap actions have to
 *    stay cheap for the expensive ones to keep working.
 *
 * Text-parse guards, per the source-grep convention in this repo
 * (behaviour is covered by the vitest suites next to each component).
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const DASHBOARD = resolve(DASHBOARD_ROOT, "components", "dashboard")

const TIER1_MODAL = resolve(DASHBOARD, "modals/confirm-action-modal.tsx")
// TIER23_MODAL (modals/delete-confirm-modal.tsx) is documented here for
// parity with the tier model above; no assertion reads it directly —
// it's exercised indirectly via the call sites below.

// Every page/dialog that must render the TIER-1 simple confirm.
const TIER1_CALL_SITES: Record<string, string> = {
  memories: resolve(DASHBOARD, "memories-dashboard.tsx"),
  schedules: resolve(DASHBOARD, "schedules-dashboard.tsx"),
  terminate: resolve(DASHBOARD, "agents/terminate-agent-dialog.tsx"),
  "task-delete": resolve(DASHBOARD, "tasks/delete-task-dialog.tsx"),
}

// Tier-3 confirm words, per page: file -> the expression that must be
// passed as `requiredWord`. Deliberately FOUR DIFFERENT values.
const TIER3_REQUIRED_WORDS: [string, string][] = [
  [resolve(DASHBOARD, "agents/purge-agent-dialog.tsx"), "agentId"],
  [resolve(DASHBOARD, "users-dashboard.tsx"), "username"],
  [resolve(DASHBOARD, "groups-dashboard.tsx"), "name"],
]

// Comments in these files legitimately DISCUSS the other tier (the
// tier table lives in a docstring), so the audits look at code only.
const BLOCK_COMMENT_RE = /\/\*[\s\S]*?\*\//g
const LINE_COMMENT_RE = /^\s*\/\/.*$/gm
const JSX_COMMENT_RE = /\{\/\*[\s\S]*?\*\/\}/g

/** File contents with comments stripped. */
function read(path: string): string {
  let src = readFileSync(path, "utf8")
  src = src.replace(JSX_COMMENT_RE, "")
  src = src.replace(BLOCK_COMMENT_RE, "")
  return src.replace(LINE_COMMENT_RE, "")
}

describe("destructive-action confirmation tiers", () => {
  it("tier-1 modal has no type-to-confirm gate", () => {
    // <ConfirmActionModal> is tier 1 BY CONSTRUCTION: if it ever grew
    // a confirmation input, every tier-1 call site would silently
    // become tier 2 and the habituation argument would collapse.
    const src = read(TIER1_MODAL)
    expect(
      src.includes("requiredWord"),
      "ConfirmActionModal must not grow a type-to-confirm word — that " +
        "is what <DeleteConfirmModal> is for",
    ).toBe(false)
    expect(
      src.includes("<Input"),
      "ConfirmActionModal must not render a text input; tier 1 is a " +
        "single click",
    ).toBe(false)
  })

  it("tier-1 call sites use the shared modal", () => {
    // No hand-rolled simple confirms. Tasks / Schedules / Terminate
    // each used to carry their own copy of the same {busy, error} +
    // Cancel/destructive-confirm state machine (architecture review
    // Class 5).
    const failures: string[] = []
    for (const [slug, path] of Object.entries(TIER1_CALL_SITES)) {
      const src = read(path)
      if (!src.includes("<ConfirmActionModal")) {
        failures.push(`${slug} (${path}) does not render <ConfirmActionModal>`)
      }
    }
    expect(failures, ["tier-1 call sites drifted:", ...failures].join("\n  ")).toEqual([])
  })

  it("memory delete is tier 1", () => {
    // Memory delete was DOWNGRADED from type-DELETE to a simple
    // confirm: single row, bounded cascade (one RAG source), value
    // visible in the modal's details slot, and the keys that are
    // genuinely unrecoverable are gated server-side by `force_delete`
    // in `project_context_tools.py`. It is also the highest-frequency
    // delete in the product, which is exactly where habituation is
    // bought.
    const src = read(resolve(DASHBOARD, "memories-dashboard.tsx"))
    expect(src.includes("<ConfirmActionModal")).toBe(true)
    expect(
      src.includes("DeleteConfirmModal"),
      "memories must not re-acquire the type-DELETE gate",
    ).toBe(false)
    // It must still name the key it is about to delete.
    expect(src.includes("context_key")).toBe(true)
  })

  it("task delete escalates only on a real cascade", () => {
    // Per-invocation escalation: the tier follows the blast radius of
    // THIS click, not the entity type.
    const src = read(resolve(DASHBOARD, "tasks/delete-task-dialog.tsx"))
    expect(
      src.includes("<ConfirmActionModal") && src.includes("<DeleteConfirmModal"),
      "the task delete dialog must be able to render BOTH tiers",
    ).toBe(true)
    expect(
      src.includes("requires_force"),
      "the tier must be chosen from the server's blast-radius preview",
    ).toBe(true)
    expect(src.includes("getTaskDeletePreview")).toBe(true)
  })

  it("purge is tier 3 on the agent id", () => {
    // Purge has no un-purge endpoint and agent ids are visually
    // near-identical (`agent-a959a84c…` / `agent-a92d2d9ef…`) while
    // terminated rows render interleaved with active ones. Typing
    // `DELETE` proves intent but not TARGET.
    const src = read(resolve(DASHBOARD, "agents/purge-agent-dialog.tsx"))
    expect(
      /requiredWord=\{agentId/.test(src),
      "purge must require the agent id, not a generic word",
    ).toBe(true)
    expect(
      /^\s*matchCase\s*$/m.test(src),
      "purge's confirm word must be case-sensitive",
    ).toBe(true)
  })

  it("tier-3 confirm words stay polymorphic", () => {
    // The four tier-3 gates must demand FOUR DIFFERENT strings. This
    // is the test that fails if someone "unifies" them on `DELETE`.
    const words: string[] = []
    const failures: string[] = []
    for (const [path, expected] of TIER3_REQUIRED_WORDS) {
      const src = read(path)
      const m = src.match(/requiredWord=\{([A-Za-z0-9_.?\s]+?)[}\s]/)
      if (m === null) {
        failures.push(`${path} passes no requiredWord expression`)
        continue
      }
      const expr = m[1]!.trim()
      if (!expr.includes(expected)) {
        failures.push(
          `${path} confirms on ${JSON.stringify(expr)}, expected something derived ` +
            `from ${JSON.stringify(expected)}`,
        )
      }
      if (/requiredWord="DELETE"/.test(src)) {
        failures.push(
          `${path} collapsed its confirm word to the uniform ` +
            `"DELETE" — see the polymorphism note in this module`,
        )
      }
      words.push(expr)
    }
    expect(failures, ["tier-3 polymorphism broken:", ...failures].join("\n  ")).toEqual([])
    expect(
      new Set(words).size,
      `tier-3 confirm words must all differ, got ${JSON.stringify(words)}`,
    ).toBe(words.length)
  })
})
