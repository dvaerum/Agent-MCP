import { readFileSync } from "node:fs"
import { resolve } from "node:path"

// The Messages page + every module it was split into (Wave 5:
// refactor/w5-messages). The TS counterpart of tests/dashboard_sources.py's
// MESSAGES_SOURCES — the source-text guards in messages-ux.test.ts /
// placeholder-subject.test.ts assert properties of the PAGE (its column
// spec, compose form, filter bar), not of any single file, so they read
// the page and its satellites as one blob. Keep this list in sync with
// the Python copy when a satellite is added or removed.
const DASHBOARD_ROOT = resolve(__dirname, "..", "..")

export const MESSAGES_SOURCES = [
  "components/dashboard/messages-dashboard.tsx",
  "components/dashboard/messages/messages-api.ts",
  "components/dashboard/messages/use-messages-columns.tsx",
  "components/dashboard/messages/compose-message-modal.tsx",
  "components/dashboard/messages/view-message-modal.tsx",
  "components/dashboard/messages/message-delete-preview.tsx",
  "components/dashboard/messages/messages-pagination.tsx",
  "components/dashboard/messages-mobile-list.tsx",
] as const

export function messagesPageSource(): string {
  return MESSAGES_SOURCES.map((rel) =>
    readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8"),
  ).join("\n")
}
