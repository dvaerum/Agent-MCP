import { readFileSync } from "node:fs"
import { resolve } from "node:path"

// The Groups page + every module it was split into (Wave 5:
// fix/w5-groups). The TS counterpart of tests/dashboard_sources.py's
// GROUPS_SOURCES — the source-text guards (e.g. ux-polish.test.ts's
// UX-08 group-delete gate) assert properties of the PAGE, not of any
// single file, so they read the page and its satellites as one blob.
// Keep this list in sync with the Python copy when a satellite is added
// or removed.
const DASHBOARD_ROOT = resolve(__dirname, "..", "..")

export const GROUPS_SOURCES = [
  "components/dashboard/groups-dashboard.tsx",
  "components/dashboard/groups/groups-api.ts",
  "components/dashboard/groups/use-groups-columns.tsx",
  "components/dashboard/groups/add-group-modal.tsx",
  "components/dashboard/groups/edit-group-modal.tsx",
  "components/dashboard/groups/add-member-modal.tsx",
  "components/dashboard/groups/group-detail-panel.tsx",
  "components/dashboard/groups/group-capabilities-section.tsx",
] as const

export function groupsPageSource(): string {
  return GROUPS_SOURCES.map((rel) =>
    readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8"),
  ).join("\n")
}
