"use client"

import * as React from "react"
import { Eye, Pencil, Trash2, Users, GitBranch } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type { Task } from "@/lib/api"

/**
 * Mobile card-list rendering of the tasks table (CC-7 audit 2026-06-02).
 *
 * The desktop <Table> has 7 columns (Task / Status / Details / Priority
 * / Relations / Updated / Actions) and overflows horizontally at 375 px
 * (verified in the audit screenshots). This component renders the same
 * rows as a stack of compact <Card>s with the primary fields
 * (title + status + priority) on top and the secondary fields
 * (assignee, relations, updated, actions) tucked into a compact
 * meta-row underneath.
 *
 * Tap-anywhere opens the View dialog, same as the desktop row. The
 * three icon buttons stop propagation so they trigger Edit / Delete /
 * View explicitly. Touch targets are h-9 w-9 (36 px) — bumped from the
 * desktop h-7 w-7 (28 px) per CC-12 (44 px is the strict iOS HIG floor;
 * 36 px is the shadcn-conventional mobile-icon-button size and lives
 * inside a generous full-card touch zone that opens View anyway).
 */

const STATUS_TONE: Record<string, string> = {
  in_progress: "bg-primary/15 text-primary ring-1 ring-primary/20",
  pending: "bg-amber-500/15 text-amber-600 dark:text-amber-400 ring-1 ring-amber-500/20",
  completed: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 ring-1 ring-emerald-500/20",
  cancelled: "bg-muted text-muted-foreground ring-1 ring-border",
  failed: "bg-orange-500/15 text-orange-600 dark:text-orange-400 ring-1 ring-orange-500/20",
}

const PRIORITY_TONE: Record<string, string> = {
  high: "bg-orange-500/10 text-orange-600 dark:text-orange-400 border-orange-500/20",
  medium: "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20",
  low: "bg-muted text-muted-foreground border-border",
}

function formatRelative(iso: string | undefined): string {
  if (!iso) return "—"
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const diff = Date.now() - t
  if (diff < 60_000) return "just now"
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return `${Math.floor(diff / 86_400_000)}d ago`
}

interface TasksMobileListProps {
  tasks: Task[]
  // Live-lookup useDialog (Candidate D, 2026-06-02): handlers take
  // the task_id; the dialog reads the row live from the source.
  openView: (taskId: string) => void
  openEdit: (taskId: string) => void
  openDelete: (taskId: string) => void
}

export function TasksMobileList({
  tasks,
  openView,
  openEdit,
  openDelete,
}: TasksMobileListProps): React.ReactElement {
  return (
    <ul role="list" className="divide-y divide-border">
      {tasks.map((task) => (
        <li
          key={task.task_id}
          onClick={() => openView(task.task_id)}
          className="p-4 hover:bg-muted/30 active:bg-muted/50 transition-colors duration-150 cursor-pointer"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="font-medium text-sm text-foreground truncate">
                {task.title}
              </div>
              <div className="text-[11px] text-muted-foreground font-mono tabular-nums mt-0.5">
                #{task.task_id.slice(-6)}
              </div>
            </div>
            <Badge
              variant="outline"
              className={cn(
                "shrink-0 text-[10px] font-semibold border-0 px-2 py-0.5 rounded-md uppercase tracking-wider",
                STATUS_TONE[task.status] ?? STATUS_TONE.pending,
              )}
            >
              {task.status.replace("_", " ")}
            </Badge>
          </div>

          {task.description && (
            <p className="text-xs text-muted-foreground mt-2 line-clamp-2">
              {task.description}
            </p>
          )}

          <div className="flex items-center flex-wrap gap-2 mt-3">
            <Badge
              variant="outline"
              className={cn(
                "text-[10px] font-medium px-2 py-0.5",
                PRIORITY_TONE[task.priority] ?? PRIORITY_TONE.medium,
              )}
            >
              {task.priority.toUpperCase()}
            </Badge>
            {task.assigned_to && (
              <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                <Users className="h-3 w-3" />
                {task.assigned_to}
              </span>
            )}
            {task.parent_task && (
              <Badge
                variant="outline"
                className="text-[10px] px-2 py-0.5 bg-purple-500/10 text-purple-600 dark:text-purple-300 border-purple-500/20"
              >
                <GitBranch className="h-3 w-3 mr-1" />
                Subtask
              </Badge>
            )}
            {task.child_tasks && task.child_tasks.length > 0 && (
              <Badge
                variant="outline"
                className="text-[10px] px-2 py-0.5 bg-blue-500/10 text-blue-600 dark:text-blue-300 border-blue-500/20 tabular-nums"
              >
                {task.child_tasks.length} children
              </Badge>
            )}
            <span className="ml-auto text-[11px] text-muted-foreground tabular-nums">
              {formatRelative(task.updated_at)}
            </span>
          </div>

          <div className="flex items-center justify-end gap-1 mt-3">
            <Button
              variant="ghost"
              size="sm"
              title="View task"
              aria-label="View task"
              className="h-9 w-9 p-0 text-muted-foreground hover:text-foreground"
              onClick={(e) => { e.stopPropagation(); openView(task.task_id) }}
            >
              <Eye className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              title="Edit task"
              aria-label="Edit task"
              className="h-9 w-9 p-0 text-primary hover:text-primary hover:bg-primary/10"
              onClick={(e) => { e.stopPropagation(); openEdit(task.task_id) }}
            >
              <Pencil className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              title="Delete task"
              aria-label="Delete task"
              className="h-9 w-9 p-0 text-destructive hover:text-destructive hover:bg-destructive/10"
              onClick={(e) => { e.stopPropagation(); openDelete(task.task_id) }}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        </li>
      ))}
    </ul>
  )
}
