"use client"

import type { Task } from "@/lib/api"

// Tasks-resource constants + pure presentation helpers (Wave 5
// extraction — the tasks twin of messages/messages-api.ts). Tasks have
// no bespoke fetch helper (they call `apiClient.*` directly), so unlike
// messages-api.ts there is no `callTasks`; this file holds the badge
// class maps + the small parse helper the extracted modals + column spec
// all share, so those styles can never drift between the row, the mobile
// card and the View / Edit dialogs.

// Status / priority colour helpers shared by the column spec + the
// View / Edit dialogs. Kept in one place so the styles match across
// every task surface.
export const statusBadgeClass = (status: Task["status"]): string => {
  const map: Record<string, string> = {
    in_progress: "bg-primary/15 text-primary ring-1 ring-primary/20",
    pending: "bg-amber-500/15 text-amber-500 dark:text-amber-300 ring-1 ring-amber-500/20",
    completed: "bg-emerald-500/15 text-emerald-500 dark:text-emerald-300 ring-1 ring-emerald-500/20",
    cancelled: "bg-muted text-muted-foreground ring-1 ring-border",
    failed: "bg-orange-500/15 text-orange-500 dark:text-orange-300 ring-1 ring-orange-500/20",
  }
  return map[status] || map.pending || ""
}

export const priorityBadgeClass = (priority: Task["priority"]): string => {
  const map: Record<string, string> = {
    high: "bg-orange-500/10 text-orange-500 dark:text-orange-300 border-orange-500/20",
    medium: "bg-amber-500/10 text-amber-500 dark:text-amber-300 border-amber-500/20",
    low: "bg-muted text-muted-foreground border-border",
  }
  return map[priority] || map.medium || ""
}

// Same author tombstone rendering used by the Agents page: a deleted
// agent id like `[deleted-foo]` renders grey + italic so admins can
// spot it without clicking through.
export const isTombstone = (value: string | undefined | null): boolean =>
  typeof value === "string" && /^\[deleted-.+\]$/.test(value)

// A single comment entry as stored on a task (server appends
// `{timestamp, author, content}` on every `notes: str` update).
export interface TaskComment {
  timestamp: string
  author: string
  content: string
}

// Parse the `notes` field into a typed array.
//
// W4-followup(A) hoisted `child_tasks` / `depends_on_tasks` to `string[]`
// at the lib/api boundary (`normalizeTask`), so those two fields are
// consumed directly and no longer need a defensive parse. `notes` is
// NOT normalized there — the wire still hands it back as either a
// JSON-encoded string or a real array — so this small parse survives
// (the last remnant of the page's old `parseJsonField`).
export const parseTaskComments = (field: unknown): TaskComment[] => {
  const arr = Array.isArray(field)
    ? field
    : typeof field === "string"
      ? (() => {
          try {
            const parsed = JSON.parse(field)
            return Array.isArray(parsed) ? parsed : []
          } catch {
            return []
          }
        })()
      : []
  return arr as TaskComment[]
}
