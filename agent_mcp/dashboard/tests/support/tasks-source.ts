import { readFileSync } from "node:fs"
import { resolve } from "node:path"

// The Tasks page + every module it was split into (Wave 5:
// refactor/w5-tasks). The TS counterpart of tests/dashboard_sources.py's
// TASKS_SOURCES — the source-text guards (e.g. tasks-clamp.test.ts)
// assert properties of the PAGE (its clamp wiring, pagination range
// label), not of any single file, so they read the page and its
// satellites as one blob. Keep this list in sync with the Python copy
// when a satellite is added or removed.
const DASHBOARD_ROOT = resolve(__dirname, "..", "..")

export const TASKS_SOURCES = [
  "components/dashboard/tasks-dashboard.tsx",
  "components/dashboard/tasks/tasks-api.ts",
  "components/dashboard/tasks/use-tasks-columns.tsx",
  "components/dashboard/tasks/create-task-modal.tsx",
  "components/dashboard/tasks/view-task-dialog.tsx",
  "components/dashboard/tasks/edit-task-dialog.tsx",
  "components/dashboard/tasks/delete-task-dialog.tsx",
  "components/dashboard/tasks/tasks-pagination.tsx",
  "components/dashboard/tasks-mobile-list.tsx",
] as const

export function tasksPageSource(): string {
  return TASKS_SOURCES.map((rel) =>
    readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8"),
  ).join("\n")
}
