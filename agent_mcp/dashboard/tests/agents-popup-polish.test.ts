/**
 * Regression guards for Phase 7-UX2: Agents page row-click + popup polish
 * + MCP-onboarding tabs.
 *
 * This PR brings the Agents page View dialog up to parity with the Tasks
 * page polish (PR #54): clicking anywhere on a row body opens the View
 * dialog, the dialog is wider + viewport-capped + has a sticky header /
 * footer with a single scrollable body region, and long tokens / snippets
 * wrap inside the box instead of overflowing.
 *
 * In addition, the View dialog grows a new MCP-onboarding section: a
 * shadcn Tabs primitive with one tab per supported MCP client (Claude
 * Code, OpenCode, Cursor, Cline, Zed, Continue.dev, Generic JSON). Each
 * tab shows the copy-paste-ready config snippet for THIS agent, with a
 * copy button and a localStorage-persisted "preferred client" memory.
 *
 * Text-parse regression guards (house source-grep convention). No jsdom
 * in this repo for this kind of guard; behaviour is verified by
 * `npm run build` plus Firefox MCP e2e.
 *
 * Note on the body-slicing regex: several checks below bound their
 * assertions to "the AgentDetailDialog body" via
 * `/const AgentDetailDialog = [\s\S]*?<\/DialogFooter>/`. Post-refactor,
 * `agent-detail-dialog.tsx` itself no longer renders a `<DialogFooter>`
 * (it composes the shared `<ViewDialogFooter>` instead) — the first
 * literal `</DialogFooter>` in the concatenated Agents-page blob is
 * actually inside `register-agent-modal.tsx`, several satellites later.
 * The non-greedy regex still matches (it just captures a wider slice
 * that happens to still contain everything these assertions look for),
 * so the guard still holds; this is carried over unmodified from the
 * original Python source-grep test, not something introduced by this
 * port.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { agentsPageSource as readAgents } from "./support/agents-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const readDashboard = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// Post-<DataTablePage> migration the Agents page is a page module plus a
// directory of satellites (register / detail / edit / terminate / purge
// dialogs, the column spec, the snippet builder). These guards are about
// the PAGE, so they read all of it (see tests/support/agents-source.ts).

/** Slice out the `const AgentDetailDialog = ... </DialogFooter>` window. */
function detailDialogBody(src: string): string {
  const m = src.match(/const AgentDetailDialog = [\s\S]*?<\/DialogFooter>/)
  expect(m, "could not locate AgentDetailDialog body").not.toBeNull()
  return m![0]
}

// ---------- Imports ------------------------------------------------

describe("agents-dashboard imports", () => {
  it("imports the Tabs primitive", () => {
    // The shadcn `<Tabs>` primitive must be imported so the
    // MCP-onboarding section can render one tab per client.
    const src = readAgents()
    expect(
      src.includes("@/components/ui/tabs"),
      "agents-dashboard.tsx must import the shadcn Tabs primitive " +
        "(@/components/ui/tabs) for the MCP-onboarding tabbed section",
    ).toBe(true)
    // Spot-check the actual named imports we use.
    for (const name of ["Tabs", "TabsList", "TabsTrigger", "TabsContent"]) {
      expect(
        src.includes(name),
        `agents-dashboard.tsx must import ${name} from @/components/ui/tabs`,
      ).toBe(true)
    }
  })

  it("imports useDialog", () => {
    // The View dialog migration mirrors PR #59's useDialog<T>() pattern
    // — the import must already be there (it was added in PR #59), but
    // keep the guard so we don't lose it during the polish refactor.
    const src = readAgents()
    expect(
      src.includes("useDialog"),
      "agents-dashboard.tsx must use the useDialog<T>() hook for the " +
        "View dialog state (see PR #59)",
    ).toBe(true)
    expect(
      src.includes("@/hooks/use-dialog"),
      "useDialog import must come from @/hooks/use-dialog",
    ).toBe(true)
  })

  it("imports the Label primitive", () => {
    // Labels above values in the View dialog should use the shadcn
    // `Label` primitive — matches Tasks page polish (PR #54).
    const src = readAgents()
    expect(
      src.includes("@/components/ui/label"),
      "shadcn Label must be imported for the polished View dialog",
    ).toBe(true)
  })

  it("imports the Copy icon", () => {
    // The per-tab copy button uses lucide's `Copy` icon.
    const src = readAgents()
    // Copy is already imported (used on the row token). Guard it stays
    // — the new MCP-onboarding tabs depend on it.
    expect(
      /\bCopy\b/.test(src),
      "agents-dashboard.tsx must import lucide Copy icon for the " +
        "per-tab copy button",
    ).toBe(true)
  })

  it("imports project-context", () => {
    // The MCP URL must come from the path-prefix adapter (PR #56).
    const src = readAgents()
    expect(
      src.includes("@/lib/project-context") || src.includes("projectContext"),
      "agents-dashboard.tsx must consume projectContext from " +
        "@/lib/project-context to derive the MCP URL",
    ).toBe(true)
  })
})

// ---------- Row click opens the View dialog -----------------------

describe("row click opens the View dialog", () => {
  it("wires onRowClick / mobile onClick to the detail-dialog opener", () => {
    // Clicking anywhere on a row body must open the View dialog —
    // mirrors the Tasks page row-click pattern.
    //
    // Post-migration the desktop row shell belongs to
    // `<ResponsiveDataTable>`, so the page expresses this by handing
    // `<DataTablePage>` an `onRowClick`; the mobile card keeps its own
    // `onClick`. Both must point at the detail-dialog opener.
    const page = readDashboard("components/dashboard/agents-dashboard.tsx")
    expect(
      /onRowClick=\{handleSelectAgent\}/.test(page),
      "agents-dashboard.tsx must pass onRowClick={handleSelectAgent} to " +
        "<DataTablePage> so the desktop row body opens the View dialog " +
        "(same as the eye icon)",
    ).toBe(true)
    const mobile = readDashboard(
      "components/dashboard/agents-mobile-list.tsx",
    )
    expect(
      /onClick=\{\(\)\s*=>\s*openView\(agent\)\}/.test(mobile),
      "the mobile agent card must call openView(agent) on tap",
    ).toBe(true)
  })

  it("has cursor-pointer on clickable rows", () => {
    // A clickable row must visually advertise its clickability.
    //
    // The desktop row's affordance is owned by `<ResponsiveDataTable>`
    // (which adds `cursor-pointer` whenever `onRowClick` is set); the
    // mobile card declares its own.
    const shared = readDashboard(
      "components/dashboard/shared/responsive-data-table.tsx",
    )
    expect(
      shared.includes("cursor-pointer"),
      "ResponsiveDataTable must declare cursor-pointer on clickable " +
        "rows so the affordance is visible",
    ).toBe(true)
    expect(
      readAgents().includes("cursor-pointer"),
      "the mobile agent card must declare cursor-pointer",
    ).toBe(true)
  })

  it("stops propagation on row action buttons", () => {
    // The per-row action icon buttons (View / Edit / Terminate /
    // Restore / Purge) MUST call `e.stopPropagation()` in their
    // onClick — otherwise their click bubbles up to the row body and
    // opens the View dialog on top of the destructive action.
    //
    // Pattern lifted from tasks-dashboard.tsx:
    //   onClick={(e) => { e.stopPropagation(); onSelect(agent) }}
    const src = readAgents()
    // Count: there must be at least one stopPropagation call inside the
    // row-action buttons block. A single occurrence is the floor — in
    // practice we expect five (one per action button).
    expect(
      src.includes("stopPropagation"),
      "row-action buttons must call e.stopPropagation() to prevent " +
        "bleed-through into the TableRow body onClick",
    ).toBe(true)
    // Stricter check: there should be multiple stopPropagation calls
    // (one per action button, ≥3 covers the always-rendered ones).
    const count = src.split("stopPropagation").length - 1
    expect(
      count >= 3,
      `expected ≥3 stopPropagation calls (one per action button); found ${count}`,
    ).toBe(true)
  })
})

// ---------- Dialog polish: width, height cap, scrollable body -----

describe("View dialog polish", () => {
  it("uses the sm:!max-w-3xl override", () => {
    // The View dialog DialogContent must use `sm:!max-w-3xl` (the
    // Tailwind important variant) to override the base DialogContent's
    // `sm:max-w-lg`, same as PR #54 did for the Tasks page.
    const src = readAgents()
    expect(
      /const AgentDetailDialog = [\s\S]*?DialogContent[^>]*sm:!max-w-3xl/.test(
        src,
      ),
      "AgentDetailDialog DialogContent must use sm:!max-w-3xl to " +
        "override base sm:max-w-lg",
    ).toBe(true)
  })

  it("caps height at 90dvh", () => {
    // The DialogContent must cap at 90dvh so very long snippets don't
    // push the modal past the viewport. `dvh`, not `vh` — see
    // docs/learnings/dashboard-dialog-mobile-clipping.md.
    const src = readAgents()
    expect(
      /const AgentDetailDialog = [\s\S]*?DialogContent[^>]*max-h-\[90dvh\]/.test(
        src,
      ),
      "AgentDetailDialog DialogContent must declare max-h-[90dvh]",
    ).toBe(true)
  })

  it("body is a single flex scroll region", () => {
    // The body region of the View dialog must use the
    // `flex-1 min-h-0 overflow-y-auto` triplet so it's the single
    // scroll region inside a flex-column DialogContent (header + footer
    // pinned via flex-shrink-0). Same idiom as PR #54.
    const body = detailDialogBody(readAgents())
    expect(
      body.includes("flex-1 min-h-0 overflow-y-auto"),
      "View dialog body must declare `flex-1 min-h-0 overflow-y-auto` " +
        "as the single scroll region",
    ).toBe(true)
    expect(
      body.includes("flex-shrink-0"),
      "View dialog header + footer must be flex-shrink-0 so the body " +
        "is the only thing that scrolls",
    ).toBe(true)
  })

  it("long values wrap anywhere", () => {
    // Tokens are 32-hex blobs and snippet bodies can be long URLs —
    // they MUST use `[overflow-wrap:anywhere]` so they wrap inside the
    // box instead of stretching the dialog horizontally.
    const body = detailDialogBody(readAgents())
    expect(
      body.includes("[overflow-wrap:anywhere]"),
      "long values (token, snippets) must use [overflow-wrap:anywhere]",
    ).toBe(true)
  })

  it("title uses line-clamp-3, not truncate", () => {
    // The dialog title shows the agent_id; long ids should wrap up to
    // 3 lines (`line-clamp-3 break-words`) rather than being silently
    // truncated with `truncate`.
    const body = detailDialogBody(readAgents())
    expect(
      body.includes("line-clamp-3"),
      "View dialog title must use line-clamp-3 (not truncate) so long " +
        "agent_ids wrap instead of being silently truncated",
    ).toBe(true)
    expect(
      body.includes("break-words"),
      "View dialog title must use break-words alongside line-clamp-3",
    ).toBe(true)
  })

  it("renders the Label primitive", () => {
    // The polished View dialog must use the shadcn `<Label>` for field
    // labels above values.
    const body = detailDialogBody(readAgents())
    expect(
      /<Label\b/.test(body),
      "View dialog must render the shadcn <Label> primitive for " +
        "labels above values",
    ).toBe(true)
  })
})

// ---------- MCP-onboarding tabbed section --------------------------

describe("MCP-onboarding tabbed section", () => {
  it("contains Tabs JSX", () => {
    // The MCP-onboarding section must use the shadcn `<Tabs>` primitive
    // (Radix-backed).
    const body = detailDialogBody(readAgents())
    expect(
      body.includes("<Tabs"),
      "View dialog body must render a <Tabs> element for the " +
        "MCP-onboarding section",
    ).toBe(true)
  })

  it("has all seven client tabs", () => {
    // One TabsTrigger per supported client.
    const body = detailDialogBody(readAgents())
    for (const value of [
      "claude-code",
      "opencode",
      "cursor",
      "cline",
      "zed",
      "continue",
      "generic",
    ]) {
      const triggerRe = new RegExp(`<TabsTrigger\\s+value="${value}"`)
      expect(
        triggerRe.test(body),
        `View dialog must include a <TabsTrigger value="${value}"> for ` +
          "the MCP-onboarding section",
      ).toBe(true)
      const contentRe = new RegExp(`<TabsContent\\s+value="${value}"`)
      expect(
        contentRe.test(body),
        `View dialog must include a <TabsContent value="${value}"> ` +
          "with the snippet for that client",
      ).toBe(true)
    }
  })

  it("has a copy button", () => {
    // Each tab needs a copy button — the lucide Copy icon must be
    // rendered inside the MCP-onboarding section.
    const body = detailDialogBody(readAgents())
    // The body already contains a token-copy <Copy /> — make sure the
    // onboarding section also includes one. Heuristic: there must be at
    // least 2 <Copy occurrences in the body (token + ≥1 snippet copy).
    const count = body.split("<Copy").length - 1
    expect(
      count >= 2,
      "View dialog body must include at least 2 <Copy /> icons (token " +
        "copy + MCP-onboarding snippet copy)",
    ).toBe(true)
  })

  it("persists the active tab in localStorage", () => {
    // The active tab must persist across reloads — we key it under
    // `agent-mcp-popup-active-client` so a user's "I always use
    // OpenCode" preference is sticky.
    const src = readAgents()
    expect(
      src.includes("agent-mcp-popup-active-client"),
      "active-tab persistence must use the localStorage key " +
        "'agent-mcp-popup-active-client'",
    ).toBe(true)
    expect(
      src.includes("localStorage"),
      "active-tab persistence must reference localStorage",
    ).toBe(true)
  })

  it("uses the fixed 'agent-mcp' server name", () => {
    // The snippet server name must be the fixed string `agent-mcp`
    // (NOT namespaced `agent-mcp-${agent.agent_id}`). This matches the
    // user's .claude.json convention so the slash-command prefix is
    // `agent-mcp:`; a single fixed key is fine because .mcp.json entries
    // are scoped per cwd/project. The regex guards against a revert to
    // the `agent-mcp-${...}` interpolated form.
    const src = readAgents()
    // buildSnippet owns the server-name literal; assert it binds `name`
    // to the fixed 'agent-mcp' string.
    expect(
      /const\s+name\s*=\s*'agent-mcp'/.test(src),
      "buildSnippet must set `const name = 'agent-mcp'` (the fixed " +
        "server key), matching the user's .claude.json convention",
    ).toBe(true)
    // And guard against a revert to the interpolated per-agent_id form.
    expect(
      /agent-mcp-\$\{[^}]*agent[^}]*\.agent_id/.test(src),
      "snippet server name must be the fixed `agent-mcp`, not the " +
        "namespaced `agent-mcp-${agent.agent_id}` form",
    ).toBe(false)
  })

  it("uses Streamable HTTP transport", () => {
    // The snippets must declare Streamable HTTP transport — the
    // backend gates `/mcp` to POST/GET/DELETE per MCP spec rev
    // 2025-03-26 (PR #61). The Claude Code CLI snippet uses
    // `--transport http`; the JSON snippets use `"type": "http"`.
    const src = readAgents()
    // File-level check: snippet templates live in a sibling helper
    // (buildSnippet) so we don't constrain them to the dialog body.
    expect(
      src.includes("--transport http") || src.includes('"type": "http"'),
      "MCP snippets must declare http transport (--transport http for " +
        'the CLI or "type": "http" in JSON configs)',
    ).toBe(true)
  })

  it("uses Bearer authorization", () => {
    // Every snippet must send the agent's token as
    // `Authorization: Bearer <token>`.
    const body = detailDialogBody(readAgents())
    // The literal "Bearer " prefix should be present in the snippet
    // template strings.
    expect(
      body.includes("Bearer "),
      "snippets must use Authorization: Bearer <token>",
    ).toBe(true)
  })

  it("snippet URL uses the /mcp endpoint", () => {
    // The snippet URL must point at the `/mcp` Streamable HTTP endpoint
    // (not the legacy `/sse`).
    const body = detailDialogBody(readAgents())
    expect(
      body.includes("/mcp"),
      "snippet URL must point at the /mcp Streamable HTTP endpoint",
    ).toBe(true)
  })
})

// ---------- RegisterAgentModal snippet container --------------------

describe("RegisterAgentModal snippet container", () => {
  it("has min-w-0 on the snippet container", () => {
    // RegisterAgentModal pane-2 wraps the .mcp.json snippet in a grid/
    // flex child. Without `min-w-0` that child inherits
    // `min-width:auto` and refuses to shrink below the <pre>'s
    // min-content (the long unbreakable URL), so the dialog balloons
    // past `sm:!max-w-lg` and the snippet bleeds over the agents table.
    // Lock `min-w-0` on the snippet container so `overflow-x-auto`
    // engages instead.
    const src = readAgents()
    // The pane-2 block renders {result.mcp_snippet} inside a <pre>. Grab
    // the enclosing snippet <div> (the one right before that <pre>) and
    // assert it carries min-w-0.
    const block = src.match(
      /<div className="min-w-0">\s*<div className="flex items-center[\s\S]*?\{result\.mcp_snippet\}/,
    )
    expect(
      block,
      'RegisterAgentModal snippet container must be a <div ' +
        'className="min-w-0"> wrapping the {result.mcp_snippet} <pre> ' +
        "so the dialog stays at sm:!max-w-lg",
    ).not.toBeNull()
  })
})
