/**
 * UX-01 regression guard: Prompt Book variables that reference a
 * known entity (agent) or a fixed set (priority) must render pickers,
 * not free-text Inputs.
 *
 * The dashboard test baseline is deliberately node-env + source-text
 * assertions (no jsdom / RTL — see vitest.config.ts). So rather than
 * mount <PromptBuilder>, we pin the two properties that make the
 * feature work:
 *
 *   1. The catalog tags the right variables with source/type/options
 *      (and importantly does NOT tag manager-create-worker's AGENT_ID, which
 *      names a *new* agent — a picker of existing agents is wrong
 *      there).
 *   2. The builder source branches on those fields: it renders
 *      <AgentSelect> for source:'agent' and an enum <Select> for
 *      type:'enum', falling back to <Input> otherwise.
 *
 * If a future refactor drops the branch or un-tags the catalog, these
 * flip red before the UX regresses.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const REPO_ROOT = resolve(DASHBOARD_ROOT, "..", "..")
const read = (base: string, rel: string) =>
  readFileSync(resolve(base, rel), "utf8")

type Variable = {
  name: string
  source?: string
  type?: string
  options?: string[]
}
type Prompt = { id: string; variables?: Variable[] }
const catalog: { prompts: Prompt[] } = JSON.parse(
  read(REPO_ROOT, "agent_mcp/prompts/catalog.json"),
)
const promptById = (id: string) =>
  catalog.prompts.find((p) => p.id === id)!
const varOf = (promptId: string, varName: string) =>
  promptById(promptId).variables!.find((v) => v.name === varName)!

// ── 1. Catalog tagging ────────────────────────────────────────────

describe("UX-01 catalog tagging", () => {
  it("worker-init AGENT_ID is an agent picker, WORKER_TOKEN an agent-token", () => {
    expect(varOf("worker-init", "AGENT_ID").source).toBe("agent")
    expect(varOf("worker-init", "WORKER_TOKEN").source).toBe("agent-token")
  })

  it("manager-assign-task AGENT_ID is an agent picker and PRIORITY an enum", () => {
    expect(varOf("manager-assign-task", "AGENT_ID").source).toBe("agent")
    const priority = varOf("manager-assign-task", "PRIORITY")
    expect(priority.type).toBe("enum")
    // Must match the tasks API enum (agent_mcp/tools/task_tools.py).
    expect(priority.options).toEqual(["low", "medium", "high"])
  })

  it("handoff-task FROM_AGENT and TO_AGENT are agent pickers", () => {
    expect(varOf("handoff-task", "FROM_AGENT").source).toBe("agent")
    expect(varOf("handoff-task", "TO_AGENT").source).toBe("agent")
  })

  it("manager-create-worker AGENT_ID stays free-text — it names a NEW agent", () => {
    const agentId = varOf("manager-create-worker", "AGENT_ID")
    expect(agentId.source).toBeUndefined()
    expect(agentId.type).toBeUndefined()
  })
})

// ── 2. Builder renders pickers for tagged variables ───────────────

describe("UX-01 prompt builder branches on picker fields", () => {
  const src = read(
    DASHBOARD_ROOT,
    "components/dashboard/prompt-book-dashboard.tsx",
  )

  it("imports and renders <AgentSelect> for source:'agent'", () => {
    expect(src).toMatch(
      /import\s+\{\s*AgentSelect\s*\}\s+from\s+['"]@\/components\/dashboard\/shared\/agent-select['"]/,
    )
    expect(src).toMatch(/variable\.source === 'agent'/)
    expect(src).toMatch(/<AgentSelect/)
  })

  it("renders an enum <Select> for type:'enum' with options", () => {
    expect(src).toMatch(/variable\.type === 'enum'/)
    expect(src).toMatch(/variable\.options\.map/)
  })
})
