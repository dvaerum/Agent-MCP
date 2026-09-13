/**
 * Regression guards for the shared `<AgentSelect>` component
 * (feat/agent-select-dropdown).
 *
 * Background
 * ----------
 *
 * Dennis reported that creating a task and assigning it to an agent
 * required the admin to *type* the agent_id into a plain `<Input>`
 * (`tasks-dashboard.tsx:495-500` pre-PR). Typo-friendly, no validation
 * that the agent exists, no visibility of available agents.
 *
 * The Phase-1 audit also surfaced two adjacent issues:
 *
 * - `EditTaskDialog` (`tasks-dashboard.tsx:964-988` pre-PR) DID use a
 *   shadcn `<Select>` but sourced its agent list from
 *   `apiClient.getAgents()` — which returns every row, including
 *   `status='terminated'`. Terminated agents leaked into the dropdown.
 * - No shared `<AgentSelect>` existed; each call-site reimplemented the
 *   dropdown by hand.
 *
 * This PR introduces
 * `agent_mcp/dashboard/components/dashboard/shared/agent-select.tsx` —
 * a single shared dropdown that filters terminated agents via the
 * existing `getActiveAgents()` store helper, pins `Admin` at the top,
 * and accepts a caller-provided `noneLabel` prop so task forms can
 * render `— Unassigned —` while filter dropdowns render `— Any —`.
 * Every agent-input site is migrated to it.
 *
 * These are text-parse regression guards in the house source-grep
 * style — the dashboard ships no jsdom/RTL for this kind of guard, so
 * behaviour is exercised by `npm run build` plus a Firefox-MCP smoke
 * pass against the `nix run .#vm-dev` interactive sandbox.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { tasksPageSource } from "@/tests/support/tasks-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const readDashboard = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

const AGENT_SELECT = "components/dashboard/shared/agent-select.tsx"
const TASKS_TSX = "components/dashboard/tasks-dashboard.tsx"
const MESSAGES_TSX = "components/dashboard/messages-dashboard.tsx"

// Wave 5 (refactor/w5-tasks): the CreateTaskModal + EditTaskDialog
// (both AgentSelect adopters) moved into the `tasks/` satellite
// directory, so the Tasks page is read as the page + its satellites
// blob (tests/support/tasks-source.ts, the TS counterpart of
// tests/dashboard_sources.py's tasks_page_source()).
function read(rel: string): string {
  if (rel === TASKS_TSX) {
    return tasksPageSource()
  }
  return readDashboard(rel)
}

// ---------- Shared component exists with the contract API -----------

describe("AgentSelect component contract", () => {
  it("shared component file exists", () => {
    // The shared component must live at
    // `components/dashboard/shared/agent-select.tsx` (the same
    // `shared/` directory that already hosts `empty-state.tsx`).
    expect(() => readDashboard(AGENT_SELECT)).not.toThrow()
  })

  it("exports named component", () => {
    // `AgentSelect` must be a named export so callers can do
    // `import { AgentSelect } from '@/components/dashboard/shared/agent-select'`.
    const src = read(AGENT_SELECT)
    expect(
      /export\s+(function|const)\s+AgentSelect\b/.test(src),
      "AgentSelect must be a named export (function or const) from " +
        "agent-select.tsx",
    ).toBe(true)
  })

  it("declares the AgentSelectProps contract", () => {
    // The `AgentSelectProps` type must declare every field from the
    // locked-design contract — `value`, `onChange`, `noneLabel`,
    // `pinAdmin`, `disabled`, `required`, `placeholder`.
    const src = read(AGENT_SELECT)
    expect(
      src.includes("AgentSelectProps"),
      "AgentSelectProps type must be declared so callers have a " +
        "documented contract",
    ).toBe(true)
    for (const field of [
      "value",
      "onChange",
      "noneLabel",
      "pinAdmin",
      "disabled",
      "required",
      "placeholder",
    ]) {
      // Permit either `field:` (required) or `field?:` (optional) — the
      // plan calls all of value/onChange required and the rest
      // optional, but we don't enforce optionality here. Just that the
      // field name appears at all.
      const re = new RegExp(`\\b${field}\\??\\s*:`)
      expect(
        re.test(src),
        `AgentSelectProps must declare a \`${field}\` field per the ` +
          "plan's locked contract",
      ).toBe(true)
    }
  })

  it("reads from the active-agents store", () => {
    // The component must source its agent list from the live-fleet
    // selector that filters `status='terminated'` — live-only is the
    // locked design decision (task assignment to a terminated agent is
    // meaningless).
    //
    // Wave 6 keystone increment 1 (2026-08-11): the live agent list
    // moved off the zustand data-store onto the shared `/all-data`
    // TanStack Query. AgentSelect now composes the `useActiveAgents()`
    // hook from `lib/queries/all-data.ts` (which applies the same
    // terminated-filter) instead of `useDataStore().getActiveAgents()`.
    const src = read(AGENT_SELECT)
    expect(
      src.includes("useActiveAgents"),
      "AgentSelect must call useActiveAgents() from the shared " +
        "/all-data query so the terminated-agent leak (the EditTaskDialog " +
        "bug) cannot recur. Don't re-implement the filter inline; compose " +
        "the existing helper.",
    ).toBe(true)
    expect(
      src.includes("@/lib/queries/all-data"),
      "AgentSelect must read the live agent list from the shared " +
        "/all-data TanStack Query (lib/queries/all-data) — the single " +
        "source every dashboard consumer now shares",
    ).toBe(true)
  })

  it("docstring explains the noneLabel convention", () => {
    // A docstring at the top of the file must explain the caller-
    // provided `noneLabel` convention with examples — the component
    // itself stays neutral and the label text is context-specific
    // (`— Unassigned —` for task forms, `— Any —` for filters).
    // Documenting this prevents future contributors from baking domain
    // labels into the component.
    const src = read(AGENT_SELECT)
    // Look for a docstring-style block comment somewhere in the first
    // 60 lines that mentions noneLabel and at least one concrete
    // example label.
    const head = src.split("\n").slice(0, 60).join("\n")
    expect(
      head.includes("noneLabel"),
      "the top-of-file docstring must mention `noneLabel` so future " +
        "contributors understand the caller-provided-label convention",
    ).toBe(true)
    expect(
      head.includes("Unassigned") && head.includes("Any"),
      "the top-of-file docstring must reference the two canonical " +
        "labels (`— Unassigned —` for task forms, `— Any —` for " +
        "filters) so the convention is concrete, not abstract",
    ).toBe(true)
  })

  it("pinAdmin defaults to true", () => {
    // `pinAdmin` defaults to `true` — mirror the messages-dashboard
    // pre-PR pattern where Admin sat at the top of the From/To
    // dropdowns. The prop exists for the rare cases where Admin
    // shouldn't appear.
    const src = read(AGENT_SELECT)
    // Look for `pinAdmin = true` or `pinAdmin: true` in a default-prop
    // assignment, or `pinAdmin ?? true`, etc.
    expect(
      /pinAdmin\s*[=:]\s*true/.test(src) || /pinAdmin\s*\?\?\s*true/.test(src),
      "AgentSelect must default `pinAdmin` to true — mirror the " +
        "messages-dashboard convention where Admin pins to the top",
    ).toBe(true)
  })

  it("renders Admin inline, not via the store", () => {
    // The component must render Admin inline via the `pinAdmin` prop.
    // Don't shove Admin into the live-agents store — Admin is
    // special-cased everywhere in the codebase, and the store's job is
    // to track real worker rows. Look for an `Admin` literal in the
    // component.
    const src = read(AGENT_SELECT)
    expect(
      /['"]Admin['"]/.test(src),
      "AgentSelect must render the Admin row inline (look for the " +
        "string literal 'Admin'); don't rely on the store to inject it",
    ).toBe(true)
  })
})

// ---------- Migration: CreateTaskModal uses AgentSelect ------------

describe("CreateTaskModal migration", () => {
  it("no longer uses a text input for agent assignment", () => {
    // The CreateTaskModal's Assign-To affordance must NOT be a plain
    // `<Input placeholder="agent-01">` anymore. This is the primary
    // bug Dennis reported.
    const src = read(TASKS_TSX)
    expect(
      src.includes('placeholder="agent-01"'),
      'CreateTaskModal still uses `<Input placeholder="agent-01">` for ' +
        "Assign-To — Dennis's primary bug. Replace with " +
        '`<AgentSelect noneLabel="— Unassigned —" />`.',
    ).toBe(false)
  })

  it("tasks-dashboard imports AgentSelect", () => {
    // `tasks-dashboard.tsx` must import the shared AgentSelect — it
    // powers both CreateTaskModal and EditTaskDialog.
    const src = read(TASKS_TSX)
    expect(
      src.includes("AgentSelect") && src.includes("agent-select"),
      "tasks-dashboard.tsx must `import { AgentSelect } from " +
        "'@/components/dashboard/shared/agent-select'` — both " +
        "CreateTaskModal and EditTaskDialog migrate to it",
    ).toBe(true)
  })

  it("uses the Unassigned noneLabel", () => {
    // Both task forms must use `noneLabel="— Unassigned —"` — the label
    // is context-specific (filters use `— Any —`); task forms say
    // `Unassigned` because the underlying field is nullable assignment,
    // not a filter.
    const src = read(TASKS_TSX)
    // Match noneLabel="— Unassigned —" allowing single/double quotes
    // and any whitespace flexibility around the em-dashes.
    expect(
      /noneLabel\s*=\s*["'][^"']*Unassigned[^"']*["']/.test(src),
      'tasks-dashboard.tsx must pass `noneLabel="— Unassigned —"` to ' +
        "AgentSelect — task forms render the unassigned sentinel with " +
        "this label per the locked plan",
    ).toBe(true)
  })
})

// ---------- Migration: EditTaskDialog uses AgentSelect (bonus fix) ---

describe("EditTaskDialog migration", () => {
  it("no longer uses the unfiltered getAgents call", () => {
    // EditTaskDialog used `apiClient.getAgents().then(setAgents)` which
    // returns every row including terminated agents (the
    // terminated-leak bug). After this PR the local `agents` state +
    // its fetch effect should be gone, replaced by AgentSelect reading
    // from getActiveAgents().
    const src = read(TASKS_TSX)
    // The exact line `apiClient.getAgents().then(setAgents)` lived
    // inside an effect in EditTaskDialog at ~line 866 pre-PR.
    expect(
      src.includes("apiClient.getAgents().then(setAgents)"),
      "EditTaskDialog still calls apiClient.getAgents().then(setAgents)" +
        " — this re-introduces the terminated-agent leak. The component " +
        "should let AgentSelect read live agents from the store instead " +
        "of fetching its own list.",
    ).toBe(false)
  })
})

// ---------- Migration: messages-dashboard filter + compose ----------

describe("messages-dashboard migration", () => {
  it("imports AgentSelect", () => {
    // `messages-dashboard.tsx` must import AgentSelect — From/To filter
    // dropdowns and the Compose recipient migrate to it.
    const src = read(MESSAGES_TSX)
    expect(
      src.includes("AgentSelect") && src.includes("agent-select"),
      "messages-dashboard.tsx must import { AgentSelect } from " +
        "'@/components/dashboard/shared/agent-select' — From/To " +
        "filters and the Compose recipient migrate to it",
    ).toBe(true)
  })

  it("filter dropdowns use the Any noneLabel", () => {
    // The From/To filter dropdowns must use `noneLabel="— Any —"` —
    // filter semantics differ from task-form assignment semantics, so
    // the label is `Any` (no filter) instead of `Unassigned`.
    const src = read(MESSAGES_TSX)
    expect(
      /noneLabel\s*=\s*["'][^"']*Any[^"']*["']/.test(src),
      'messages-dashboard.tsx must pass `noneLabel="— Any —"` to the ' +
        "From/To filter AgentSelects — filter dropdowns use the `Any` " +
        "label, not `Unassigned`",
    ).toBe(true)
  })
})

// ---------- nix run .#vm-dev — interactive sandbox -----------------

describe("flake.nix vm-dev app", () => {
  it("exposes a vm-dev app", () => {
    // `flake.nix` must expose a new `vm-dev` app under
    // `apps.${system}` so `nix run .#vm-dev` boots the interactive
    // Path-B sandbox with a host-reachable port (18080) and a seed
    // dataset (Admin + one live + one terminated agent).
    const flakeSrc = readFileSync(
      resolve(DASHBOARD_ROOT, "..", "..", "flake.nix"),
      "utf8",
    )
    expect(
      flakeSrc.includes("vm-dev"),
      "flake.nix must declare `apps.${system}.vm-dev` so `nix run " +
        ".#vm-dev` boots the interactive sandbox for Firefox-MCP " +
        "smoke testing",
    ).toBe(true)
  })
})
