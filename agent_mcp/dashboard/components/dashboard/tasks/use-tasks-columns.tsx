"use client"

import React, { useEffect, useMemo, useState } from "react"
import {
  Users,
  Eye,
  Pencil,
  Trash2,
  ArrowUp,
  ArrowDown,
  Minus,
  GitBranch,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { Task } from "@/lib/api"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"
import {
  statusBadgeClass,
  priorityBadgeClass,
} from "@/components/dashboard/tasks/tasks-api"

/**
 * Column spec for the Tasks table (Wave 5 extraction — mirrors
 * `useMessagesColumns` / `useAgentColumns`).
 *
 * ONE source drives the desktop table (via `<ResponsiveDataTable>`) and,
 * through the page's `renderMobileCard`, the mobile card. Cells reproduce
 * the pre-scaffold `<CompactTaskRow>` exactly; the action-cell buttons
 * `stopPropagation` so they don't also fire the row-body onClick (open
 * View).
 */

const StatusDot = React.memo(({ status }: { status: Task["status"] }) => {
  const config = {
    // CC-2 audit 2026-06-02: dropped the teal accent + colored
    // shadows; status uses the existing primary token for in-progress
    // and shadcn semantic tokens for the muted "cancelled" state.
    // animate-pulse kept ONLY on `in_progress` and `failed` where
    // it semantically conveys "live state" — see CC-19.
    in_progress: "bg-primary animate-pulse",
    pending: "bg-amber-500",
    completed: "bg-emerald-500",
    cancelled: "bg-muted-foreground/40",
    failed: "bg-destructive animate-pulse",
  }

  return (
    <div className={cn(
      "w-2.5 h-2.5 rounded-full",
      config[status] || config.pending
    )} />
  )
})
StatusDot.displayName = "StatusDot"

const PriorityIcon = React.memo(({ priority }: { priority: Task["priority"] }) => {
  const config = {
    high: { icon: ArrowUp, className: "text-orange-500" },
    medium: { icon: Minus, className: "text-amber-500" },
    low: { icon: ArrowDown, className: "text-muted-foreground" },
  }

  const configItem = config[priority] || config.medium // fallback to medium if priority is undefined
  const { icon: Icon, className } = configItem
  return <Icon className={cn("h-4 w-4", className)} />
})
PriorityIcon.displayName = "PriorityIcon"

// The "Updated" cell keeps the hydration guard the pre-scaffold
// <CompactTaskRow> carried: a locale-formatted date differs between the
// pre-hydration and post-hydration pass, so render a placeholder until
// mount. It survives as a standalone cell because the row markup itself
// is now owned by <ResponsiveDataTable>'s column spec.
const TaskUpdatedCell = React.memo(({ value }: { value: string }) => {
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  if (!mounted) return <>...</>
  return (
    <>
      {new Date(value).toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })}
    </>
  )
})
TaskUpdatedCell.displayName = "TaskUpdatedCell"

export interface TasksColumnHandlers {
  /** Open the read-only View dialog for a row. */
  openView: (taskId: string) => void
  /** Open the Edit dialog for a row. */
  openEdit: (taskId: string) => void
  /** Open the Delete confirm dialog for a row. */
  openDelete: (taskId: string) => void
}

export function useTasksColumns(
  handlers: TasksColumnHandlers,
): Column<Task>[] {
  const { openView, openEdit, openDelete } = handlers

  return useMemo<Column<Task>[]>(() => [
    {
      id: "task",
      header: "Task",
      cellClassName: "py-3",
      cell: (task) => (
        <div className="flex items-center gap-3">
          <StatusDot status={task.status} />
          <PriorityIcon priority={task.priority} />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-sm text-foreground truncate">{task.title}</div>
            <div className="text-xs text-muted-foreground font-mono">#{task.task_id.slice(-6)}</div>
          </div>
        </div>
      ),
    },
    {
      id: "status",
      header: "Status",
      cellClassName: "py-3",
      cell: (task) => (
        <Badge
          variant="outline"
          className={cn(
            "text-xs font-semibold border-0 px-3 py-1.5 rounded-md",
            statusBadgeClass(task.status),
          )}
        >
          {task.status.replace("_", " ").toUpperCase()}
        </Badge>
      ),
    },
    {
      id: "details",
      header: "Details",
      cellClassName: "py-3 max-w-xs",
      cell: (task) => (
        <>
          <div className="text-sm text-foreground truncate">
            {task.description || "No description"}
          </div>
          {task.assigned_to && (
            <div className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
              <Users className="h-3 w-3" />
              {task.assigned_to}
            </div>
          )}
        </>
      ),
    },
    {
      id: "priority",
      header: "Priority",
      cellClassName: "py-3",
      cell: (task) => (
        <Badge
          variant="outline"
          className={cn("text-xs font-medium px-2 py-0.5", priorityBadgeClass(task.priority))}
        >
          {task.priority.toUpperCase()}
        </Badge>
      ),
    },
    {
      id: "relations",
      header: "Relations",
      cellClassName: "py-3",
      cell: (task) => (
        <div className="flex flex-wrap gap-1">
          {task.parent_task && (
            <Badge variant="outline" className="text-xs px-2 py-0.5 bg-purple-500/10 text-purple-500 dark:text-purple-300 border-purple-500/20">
              <GitBranch className="h-3 w-3 mr-1" />
              Subtask
            </Badge>
          )}
          {task.child_tasks && task.child_tasks.length > 0 && (
            <Badge variant="outline" className="text-xs px-2 py-0.5 bg-blue-500/10 text-blue-500 dark:text-blue-300 border-blue-500/20">
              {task.child_tasks.length} children
            </Badge>
          )}
        </div>
      ),
    },
    {
      id: "updated",
      header: "Updated",
      cellClassName: "py-3 text-xs text-muted-foreground font-mono",
      cell: (task) => <TaskUpdatedCell value={task.updated_at} />,
    },
    {
      id: "actions",
      header: "Actions",
      headClassName: "w-24",
      cellClassName: "py-3",
      /*
        Three distinct row icons. Each opens its own Dialog modal
        (View / Edit / Delete). stopPropagation prevents the row-
        level onClick (which opens the same View dialog) from
        firing twice when the eye icon is pressed and from opening
        the View dialog when Edit/Delete are pressed. Hover-reveal
        rides on the row's `group` class, owned by the scaffold.
      */
      cell: (task) => (
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Button
            variant="ghost"
            size="sm"
            title="View task"
            aria-label="View task"
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
            onClick={(e) => { e.stopPropagation(); openView(task.task_id) }}
          >
            <Eye className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            title="Edit task"
            aria-label="Edit task"
            className="h-9 w-9 sm:h-7 sm:w-7 p-0 text-primary hover:text-primary hover:bg-primary/10"
            onClick={(e) => { e.stopPropagation(); openEdit(task.task_id) }}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            title="Delete task"
            aria-label="Delete task"
            className="h-7 w-7 p-0 text-destructive hover:text-destructive hover:bg-destructive/10"
            onClick={(e) => { e.stopPropagation(); openDelete(task.task_id) }}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      ),
    },
  ], [openView, openEdit, openDelete])
}
