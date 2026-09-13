import { readFileSync } from "node:fs"
import { resolve } from "node:path"

// The Agents page + every module it was split into since the
// <DataTablePage> migration. The source-text guards under
// tests/*.test.ts assert properties of the PAGE (row action buttons,
// the detail dialog's layout, copy text), not of any single file, so
// they read the page and its satellites as one blob. Keep this list
// in sync when an Agents satellite is added or removed — a missing
// entry silently narrows the audit surface.
const DASHBOARD_ROOT = resolve(__dirname, "..", "..")

export const AGENTS_SOURCES = [
  "components/dashboard/agents-dashboard.tsx",
  "components/dashboard/agents/agent-columns.tsx",
  "components/dashboard/agents/agent-detail-dialog.tsx",
  "components/dashboard/agents/agent-presence.tsx",
  "components/dashboard/agents/edit-agent-dialog.tsx",
  "components/dashboard/agents/purge-agent-dialog.tsx",
  "components/dashboard/agents/register-agent-modal.tsx",
  "components/dashboard/agents/terminate-agent-dialog.tsx",
  "components/dashboard/agents-mobile-list.tsx",
  "lib/mcp-snippets.ts",
] as const

export function agentsPageSource(): string {
  return AGENTS_SOURCES.map((rel) =>
    readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8"),
  ).join("\n")
}
