import type { Agent, Task } from "@/lib/api"

/**
 * arch-r5 #8 — one status/priority color map. Replaces 4 divergent,
 * disagreeing copies (agent-details-panel's `getStatusColor`,
 * task-details-dialog's `getStatusColor`/`getPriorityColor`,
 * node-detail-panel's `statusColors`/`priorityColors`) that disagreed
 * on e.g. "running" (bg-primary vs bg-blue-500) and "completed"
 * (bg-success vs bg-green-500).
 *
 * `Agent['status']` and `Task['status']` are separate unions but
 * share vocabulary ('pending', 'failed') with the same semantic
 * meaning, and node-detail-panel keys the same lookup off either
 * `agent.status` or `task.status`. Keying the Record off their union
 * keeps an unhandled status a compile-time error for both callers.
 */
type StatusKey = Agent["status"] | Task["status"]

const STATUS_COLOR_CLASSES: Record<StatusKey, string> = {
  pending: "bg-warning/15 text-warning border-warning/30",
  running: "bg-primary/15 text-primary border-primary/30",
  in_progress: "bg-primary/15 text-primary border-primary/30",
  completed: "bg-success/15 text-success border-success/30",
  terminated: "bg-muted/50 text-muted-foreground border-border",
  cancelled: "bg-muted/50 text-muted-foreground border-border",
  failed: "bg-destructive/15 text-destructive border-destructive/30",
}

export function statusColorClasses(status: StatusKey): string {
  return STATUS_COLOR_CLASSES[status]
}

const PRIORITY_COLOR_CLASSES: Record<Task["priority"], string> = {
  low: "bg-muted text-muted-foreground border-border",
  medium: "bg-primary/10 text-primary border-primary/20",
  high: "bg-destructive/10 text-destructive border-destructive/20",
}

export function priorityColorClasses(priority: Task["priority"]): string {
  return PRIORITY_COLOR_CLASSES[priority]
}
