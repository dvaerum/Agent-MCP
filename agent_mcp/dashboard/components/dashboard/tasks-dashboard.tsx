"use client"

import React, { useState, useEffect, useCallback, useMemo } from "react"
import {
  CheckSquare, Clock, AlertCircle, Users,
  Search, Plus, Eye, Pencil, Trash2, X,
  ArrowUp, ArrowDown, Minus, CheckCircle2, Target, Zap, GitBranch
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { apiClient, Task, type TaskFilters } from "@/lib/api"
import { useServerStore } from "@/lib/stores/server-store"
import { useDialog } from "@/hooks/use-dialog"
import { useFilters } from "@/hooks/use-filters"
import { usePagedQuery } from "@/hooks/use-paged-query"
import { cn, formatRelative } from "@/lib/utils"
import { toastError } from "@/components/ui/toast"
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { DeleteTaskDialog } from "@/components/dashboard/tasks/delete-task-dialog"
import { TaskMobileCard } from "@/components/dashboard/tasks-mobile-list"
import { DataTablePage } from "@/components/dashboard/shared/data-table-page"
import type { StatsCardProps } from "@/components/dashboard/shared/stats-card"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"

// PF-1 clamp (Wave 3): GET /tasks returns the WHOLE task set with no
// server-side pagination, so the client bounds the rendered list. Same
// page size messages-dashboard uses for its server-paged list, applied
// here as an in-memory window over `filteredTasks`.
const PAGE_SIZE = 100

/**
 * Pagination footer — « Newest / Newer / Older / Oldest » plus a
 * "Showing N–M of T" range label. A client-side twin of
 * messages-dashboard's <MessagesPagination>: same button spec, same
 * two-layout (justified row on sm+, stacked 4-col grid below sm), but
 * driven by an in-memory offset over the already-fetched task list
 * rather than a server query.
 */
function TasksPagination({
  rangeStart,
  rangeEnd,
  total,
  onFirstPage,
  onLastPage,
  onNewest,
  onNewer,
  onOlder,
  onOldest,
}: {
  rangeStart: number
  rangeEnd: number
  total: number
  onFirstPage: boolean
  onLastPage: boolean
  onNewest: () => void
  onNewer: () => void
  onOlder: () => void
  onOldest: () => void
}) {
  const nav = [
    {
      key: "newest",
      label: "« Newest",
      onClick: onNewest,
      disabled: onFirstPage,
      ariaLabel: "jump to first page",
    },
    { key: "newer", label: "Newer", onClick: onNewer, disabled: onFirstPage },
    { key: "older", label: "Older", onClick: onOlder, disabled: onLastPage },
    {
      key: "oldest",
      label: "Oldest »",
      onClick: onOldest,
      disabled: onLastPage,
      ariaLabel: "jump to last page",
    },
  ]
  const button = (b: (typeof nav)[number]) => (
    <Button
      key={b.key}
      variant="outline"
      size="sm"
      onClick={b.onClick}
      disabled={b.disabled}
      aria-label={b.ariaLabel}
    >
      {b.label}
    </Button>
  )
  const range = `Showing ${rangeStart}–${rangeEnd} of ${total}`
  return (
    <>
      <div className="hidden sm:flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">{nav.slice(0, 2).map(button)}</div>
        <div className="text-xs text-muted-foreground tabular-nums">{range}</div>
        <div className="flex items-center gap-2">{nav.slice(2).map(button)}</div>
      </div>
      <div className="block sm:hidden">
        <div className="text-[11px] text-muted-foreground tabular-nums text-center mb-2">
          {range}
        </div>
        <div className="grid grid-cols-4 gap-2">{nav.map(button)}</div>
      </div>
    </>
  )
}

// Status / priority colour helpers shared by the row + the View / Edit
// modals. Keep these here so the styles match the rest of the page.
const statusBadgeClass = (status: Task['status']): string => {
  const map: Record<string, string> = {
    in_progress: "bg-primary/15 text-primary ring-1 ring-primary/20",
    pending: "bg-amber-500/15 text-amber-500 dark:text-amber-300 ring-1 ring-amber-500/20",
    completed: "bg-emerald-500/15 text-emerald-500 dark:text-emerald-300 ring-1 ring-emerald-500/20",
    cancelled: "bg-muted text-muted-foreground ring-1 ring-border",
    failed: "bg-orange-500/15 text-orange-500 dark:text-orange-300 ring-1 ring-orange-500/20",
  }
  return map[status] || map.pending || ""
}

const priorityBadgeClass = (priority: Task['priority']): string => {
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
const isTombstone = (value: string | undefined | null): boolean =>
  typeof value === 'string' && /^\[deleted-.+\]$/.test(value)

const parseJsonField = (field: unknown): unknown[] => {
  if (Array.isArray(field)) return field
  if (typeof field === 'string') {
    try {
      const parsed = JSON.parse(field)
      return Array.isArray(parsed) ? parsed : []
    } catch {
      return []
    }
  }
  return []
}

// Background-refresh interval for the tasks listing. The 60s interval
// keeps the "background refresh while tab open" semantic from
// pre-migration ``useTasksData`` — the hook itself is reactive only,
// not interval-driven, so we still wire the timer here.
//
// No ``cacheMs`` (was 30s pre task-filters): the GET /tasks fetch is
// now parameterized by the server-side filter snapshot and re-runs via
// the hook's ``deps`` when a filter changes. A global TTL on the last
// response would make ``usePagedQuery`` short-circuit that reactive
// re-fetch (see its cache-hit guard) and silently serve the *previous*
// filter's rows — so the filtered fetch must not be cached. The 60s
// background refresh still forces a fetch (``refresh()`` bypasses any
// cache), and the reactive effect only fires on server / filter
// changes, so there's nothing left for a TTL to dedupe here.
const REFRESH_INTERVAL = 60000 // 1 minute for background refresh

// Tasks-dashboard data hook — delegates the loading/error/lastFetch/
// refresh state machine to ``usePagedQuery<Task>`` (PR 5 of the
// 2026-06-09 architecture review). The underlying request still
// goes through ``apiClient.getTasks()`` (a GET ``/tasks`` that
// returns ``Task[]`` directly — no envelope, no pagination, no token
// in body), so we use the hook's ``fetchFn`` escape hatch to wrap
// it. The escape hatch threads the AbortSignal into the response so
// a slow stale fetch can't overwrite a fresh fast one. ``cacheMs``
// preserves the 30s TTL; the 60s background refresh interval still
// lives here because the hook is reactive-only by design.
const useTasksData = (serverFilters: TaskFilters) => {
  const { activeServerId, servers } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  const isConnected = !!activeServerId && activeServer?.status === 'connected'

  // Disconnected: surface an empty result without firing the fetch
  // (the wrapper short-circuits via fetchFn returning empty). The
  // hook's state machine still runs — that's fine, it just stays at
  // {data: [], total: 0, loading: false, error: null}.
  //
  // ``serverFilters`` (status / assignment / creator) drives the
  // server-side filtered GET /tasks — the single source of truth
  // shared with the backend + the MCP view_tasks tool. Its serialized
  // form is threaded into ``deps`` below so a filter change re-runs
  // the fetch.
  const fetchFn = useCallback(
    async (_signal: AbortSignal): Promise<{ data: Task[]; total: number }> => {
      if (!isConnected) {
        return { data: [], total: 0 }
      }
      try {
        const tasksData = await apiClient.getTasks(serverFilters)
        return { data: tasksData, total: tasksData.length }
      } catch (err) {
        // Pre-migration ``useTasksData`` silenced 'NO_SERVER_CONNECTED'
        // errors so a transient disconnect didn't paint a red banner
        // (the DashboardWrapper handles connection state). Preserve
        // that quirk by returning empty instead of throwing.
        if (err instanceof Error && err.message === 'NO_SERVER_CONNECTED') {
          return { data: [], total: 0 }
        }
        throw err
      }
    },
    [isConnected, serverFilters],
  )

  // Stable serialization of the filter snapshot for the reactive
  // re-fetch dependency — a fresh object identity every render would
  // thrash the effect; the value change is what must re-fetch.
  const serverFiltersKey = JSON.stringify(serverFilters)

  const {
    data: tasks,
    loading,
    error: queryError,
    refresh,
    lastFetch,
  } = usePagedQuery<Task>({
    fetchFn,
    deps: [activeServerId, serverFiltersKey],
  })

  // The pre-migration shape exposed ``error`` as ``string | null``.
  // The hook returns a real ``Error | null``; map it for backward
  // compat with the consumer below.
  const error: string | null = queryError ? queryError.message : null

  // Background refresh — separate from the hook (which is reactive
  // only). Bypass the cache by calling ``refresh()`` directly; the
  // hook treats refresh() as a force-fetch.
  useEffect(() => {
    const interval = setInterval(() => {
      refresh()
    }, REFRESH_INTERVAL)
    return () => clearInterval(interval)
  }, [refresh])

  // Live refetch on backend mutation. The operator SSE client
  // (lib/mcp-notifications.ts) dispatches a debounced
  // ``mcp:resources-updated`` window event on every
  // ``notifications/resources/updated``. The tasks list fetches its OWN
  // GET /tasks endpoint (not the data-store all-data envelope), so
  // scheduleDashboardRefresh doesn't cover it — hook the event to the
  // force-fetch refresh (backend + client already debounce).
  useEffect(() => {
    if (typeof window === "undefined") return
    const handler = () => {
      refresh()
    }
    window.addEventListener("mcp:resources-updated", handler)
    return () => window.removeEventListener("mcp:resources-updated", handler)
  }, [refresh])

  // ``lastFetch`` is ``number | null`` from the hook; the consumer
  // below assumes ``number`` (it checks ``> 0``). Coalesce.
  return useMemo(() => ({
    tasks,
    loading,
    error,
    refresh,
    lastFetch: lastFetch ?? 0,
    isConnected,
  }), [tasks, loading, error, refresh, lastFetch, isConnected])
}

const StatusDot = React.memo(({ status }: { status: Task['status'] }) => {
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
StatusDot.displayName = 'StatusDot'

const PriorityIcon = React.memo(({ priority }: { priority: Task['priority'] }) => {
  const config = {
    high: { icon: ArrowUp, className: "text-orange-500" },
    medium: { icon: Minus, className: "text-amber-500" },
    low: { icon: ArrowDown, className: "text-muted-foreground" },
  }
  
  const configItem = config[priority] || config.medium // fallback to medium if priority is undefined
  const { icon: Icon, className } = configItem
  return <Icon className={cn("h-4 w-4", className)} />
})
PriorityIcon.displayName = 'PriorityIcon'

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
      {new Date(value).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })}
    </>
  )
})
TaskUpdatedCell.displayName = 'TaskUpdatedCell'

const CreateTaskModal = React.memo(({ onCreateTask }: { onCreateTask: (data: Parameters<typeof apiClient.createTask>[0]) => void }) => {
  const [open, setOpen] = useState(false)
  const [formData, setFormData] = useState<{
    title: string
    description: string
    priority: Task['priority']
    // null = "— Unassigned —" sentinel selected (no assignment).
    // Migrated from the pre-PR `<Input>` text field to the shared
    // <AgentSelect> dropdown, which surfaces the live agent roster
    // instead of asking the admin to type an agent_id.
    assigned_to: string | null
  }>({
    title: '',
    description: '',
    priority: 'medium',
    assigned_to: null,
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!formData.title.trim()) return

    onCreateTask({
      title: formData.title.trim(),
      description: formData.description.trim() || undefined,
      priority: formData.priority,
      // AgentSelect returns string | null — the null sentinel maps
      // to "no assignment", which the create-task endpoint already
      // accepts as undefined / missing.
      assigned_to: formData.assigned_to ?? undefined,
    })

    setFormData({
      title: '',
      description: '',
      priority: 'medium',
      assigned_to: null,
    })
    setOpen(false)
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" className="bg-primary hover:bg-primary/90 text-primary-foreground transition-colors duration-150">
          <Plus className="h-4 w-4 mr-1.5" />
          Create Task
        </Button>
      </DialogTrigger>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-md bg-card border-border text-card-foreground">
        <DialogHeader>
          <DialogTitle className="text-lg">Create Task</DialogTitle>
          <DialogDescription className="text-muted-foreground">
            Define a new task for the system to execute.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              Task Title
            </label>
            <Input
              value={formData.title}
              onChange={(e) => setFormData(prev => ({ ...prev, title: e.target.value }))}
              placeholder="Analyze dataset and generate report"
              className="bg-background border-border text-foreground"
              required
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              Description
            </label>
            <Textarea
              value={formData.description}
              onChange={(e) => setFormData(prev => ({ ...prev, description: e.target.value }))}
              placeholder="Detailed task requirements and objectives..."
              className="bg-background border-border text-foreground h-20 resize-none"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
                Priority
              </label>
              <Select value={formData.priority} onValueChange={(value: Task['priority']) => setFormData(prev => ({ ...prev, priority: value }))}>
                <SelectTrigger className="bg-background border-border text-foreground">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-background border-border">
                  <SelectItem value="low">Low</SelectItem>
                  <SelectItem value="medium">Medium</SelectItem>
                  <SelectItem value="high">High</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
                Assign To
              </label>
              {/*
                Migrated 2026-06-04 (feat/agent-select-dropdown): was a
                plain text Input with placeholder hint that asked
                the admin to *type* the agent_id. Typo-friendly, no
                validation that the agent exists, no visibility of
                available agents. Now uses the shared <AgentSelect>
                which sources live agents from the data-store (filters
                terminated rows via shouldDisplayAgent) and pins Admin
                at the top. noneLabel="— Unassigned —" because the
                underlying field is a nullable assignment, not a
                filter (filters use the "Any" label).
              */}
              <AgentSelect
                value={formData.assigned_to}
                onChange={(v) => setFormData(prev => ({ ...prev, assigned_to: v }))}
                noneLabel="— Unassigned —"
              />
            </div>
          </div>
          <DialogFooter className="gap-2">
            <Button type="button" variant="outline" onClick={() => setOpen(false)} size="sm">
              Cancel
            </Button>
            <Button type="submit" size="sm" className="bg-primary hover:bg-primary/90 text-primary-foreground transition-colors duration-150">
              Create Task
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
})
CreateTaskModal.displayName = 'CreateTaskModal'

// =========================================================================
// Row-action dialogs: View / Edit / Delete
//
// Each opens a shadcn Dialog (NOT a sidebar Sheet). They mirror the
// Messages-page detail popup pattern from PR #36 and the in-flight
// agents-page UI fix.
// =========================================================================

interface RowDialogProps {
  task: Task | null
  onOpenChange: (open: boolean) => void
}

// ---------- View dialog (read-only) -------------------------------

interface ViewTaskDialogProps extends RowDialogProps {
  // Optional in-modal actions. When provided, the parent wires these to
  // close this view dialog and open the sibling edit/delete confirm
  // dialog (close-then-open avoids stacked-dialog issues).
  onEdit?: () => void
  onDelete?: () => void
}

const ViewTaskDialog = React.memo(({ task, onOpenChange, onEdit, onDelete }: ViewTaskDialogProps) => {
  const open = task !== null

  // Parse JSON-shaped optional fields safely.
  const dependencies = task ? parseJsonField(task.depends_on_tasks) : []
  const childTasks = task ? parseJsonField(task.child_tasks) : []
  const notes = task ? (parseJsonField(task.notes) as Array<{ author: string; timestamp: string; content: string }>) : []
  const createdBy: string | undefined = task ? (task as unknown as { created_by?: string }).created_by : undefined

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        Width: sm:!max-w-3xl overrides the base DialogContent's `sm:max-w-lg`
        (32rem = 512px) which otherwise wins the cascade and squeezes the
        dialog to a phone-narrow 512px on desktop. The `!` (Tailwind important)
        is required because both classes share the same specificity and the
        base class is declared later in the merged className string.
      */}
      <DialogContent className="sm:!max-w-3xl w-[calc(100vw-2rem)] bg-card border-border text-card-foreground p-0 gap-0 max-h-[90vh] flex flex-col">
        {task && (
          <>
            <DialogHeader className="px-6 pt-6 pb-4 border-b border-border flex-shrink-0">
              <DialogTitle className="flex items-start justify-between pr-8 gap-3">
                {/*
                  Title wraps onto multiple lines (max 3) rather than being
                  silently truncated. `break-words` so a long unbroken
                  task_1780… style id still wraps instead of overflowing.
                */}
                <span className="text-lg font-semibold break-words line-clamp-3 leading-snug">{task.title}</span>
                <div className="flex items-center gap-2 flex-shrink-0 pt-0.5">
                  <Badge variant="outline" className={cn("text-xs", statusBadgeClass(task.status))}>
                    {task.status.replace(/_/g, ' ')}
                  </Badge>
                  <Badge variant="outline" className={cn("text-xs", priorityBadgeClass(task.priority))}>
                    {task.priority}
                  </Badge>
                </div>
              </DialogTitle>
              <DialogDescription className="text-muted-foreground">
                Read-only view of every task field. Use the pencil icon on the row to edit.
              </DialogDescription>
            </DialogHeader>

            {/*
              Scrollable body: parent is now `flex-col` with the header +
              footer marked flex-shrink-0, so this `flex-1 min-h-0 overflow-y-auto`
              expands to fill remaining space and is the single scroll region.
              Previously `max-h-[80vh]` on the body alone could push the dialog
              past the viewport (we observed h=984 on a 1000px viewport for a
              65k-char description), so the dialog now caps at 90vh total.
            */}
            <div className="px-6 py-4 flex-1 min-h-0 overflow-y-auto space-y-4 text-sm">
              {/* Group 1: core metadata in a 2-col grid */}
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label className="text-xs text-muted-foreground uppercase tracking-wider">Status</Label>
                  <div>
                    <Badge variant="outline" className={cn("text-xs", statusBadgeClass(task.status))}>
                      {task.status.replace(/_/g, ' ')}
                    </Badge>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label className="text-xs text-muted-foreground uppercase tracking-wider">Priority</Label>
                  <div>
                    <Badge variant="outline" className={cn("text-xs", priorityBadgeClass(task.priority))}>
                      {task.priority}
                    </Badge>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label className="text-xs text-muted-foreground uppercase tracking-wider">Assigned to</Label>
                  <div className={cn("text-sm", !task.assigned_to && "text-muted-foreground italic")}>
                    {task.assigned_to || '(unassigned)'}
                  </div>
                </div>
                {createdBy && (
                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground uppercase tracking-wider">Created by</Label>
                    <div className={cn(
                      "text-sm",
                      isTombstone(createdBy) && "text-muted-foreground italic"
                    )}>
                      {createdBy}
                    </div>
                  </div>
                )}
                {task.parent_task && (
                  <div className="space-y-2 col-span-2">
                    <Label className="text-xs text-muted-foreground uppercase tracking-wider">Parent task</Label>
                    <div>
                      <Badge variant="outline" className="text-xs font-mono">
                        <GitBranch className="h-3 w-3 mr-1" />
                        {task.parent_task}
                      </Badge>
                    </div>
                  </div>
                )}
              </div>

              {/*
                Group 2: description.
                - `[overflow-wrap:anywhere]` so a 65k-char unbroken string
                  (we have one in the wild — `XXX…XXX`) wraps inside the
                  block instead of forcing the body to a giant scroll-X.
                - NO inner `max-h-[Nvh] overflow-y-auto`. The dialog body
                  (`max-h-[90vh]` + `flex-1 min-h-0 overflow-y-auto`) is
                  the single vertical scroll region; nesting another one
                  here forced users to scroll twice (PR #54's polish
                  over-corrected for monster bodies). Long descriptions
                  now flow naturally into the body scroll alongside the
                  metadata footer.
              */}
              <div className="border-t border-border pt-4 space-y-2">
                <Label className="text-xs text-muted-foreground uppercase tracking-wider">Description</Label>
                {task.description ? (
                  <pre className="text-sm whitespace-pre-wrap break-words [overflow-wrap:anywhere] font-mono text-xs leading-relaxed bg-muted/40 rounded p-3">
                    {task.description}
                  </pre>
                ) : (
                  <p className="text-sm text-muted-foreground italic">(no description)</p>
                )}
              </div>

              {/* Group 3: relations (only renders if present) */}
              {(dependencies.length > 0 || childTasks.length > 0) && (
                <div className="border-t border-border pt-4 space-y-4">
                  {dependencies.length > 0 && (
                    <div className="space-y-2">
                      <Label className="text-xs text-muted-foreground uppercase tracking-wider">Depends on</Label>
                      <div className="flex flex-wrap gap-2">
                        {dependencies.map((id, idx) => (
                          <Badge key={idx} variant="outline" className="text-xs font-mono">{String(id)}</Badge>
                        ))}
                      </div>
                    </div>
                  )}
                  {childTasks.length > 0 && (
                    <div className="space-y-2">
                      <Label className="text-xs text-muted-foreground uppercase tracking-wider">Subtasks</Label>
                      <div className="flex flex-wrap gap-2">
                        {childTasks.map((id, idx) => (
                          <Badge key={idx} variant="outline" className="text-xs font-mono">{String(id)}</Badge>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/*
                Group 4: notes — always renders, with an empty state
                when the task has none. Gating on `notes.length > 0`
                hid the section completely for empty tasks (no
                affordance, no hint the feature exists). The Add-note
                affordance lives in the Edit dialog (`apiClient.updateTask`
                with `notes: string` appends a new entry).
              */}
              <div className="border-t border-border pt-4 space-y-2">
                <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                  Notes{notes.length > 0 ? ` (${notes.length})` : ''}
                </Label>
                {notes.length > 0 ? (
                  <div className="space-y-2">
                    {notes.map((note, idx) => (
                      <div key={idx} className="bg-muted/50 rounded-lg p-3">
                        <div className="flex items-center justify-between mb-1 text-xs">
                          <span className={cn(
                            "font-medium",
                            isTombstone(note.author) && "text-muted-foreground italic"
                          )}>
                            {note.author || 'unknown'}
                          </span>
                          <span className="text-muted-foreground" title={note.timestamp}>
                            {formatRelative(note.timestamp)}
                          </span>
                        </div>
                        <p className="text-sm whitespace-pre-wrap">{note.content}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground italic">
                    No notes yet. Use the Edit dialog to add one.
                  </p>
                )}
              </div>

              {/*
                Group 5: tombstone metadata footer.
                Each row is a 2-col grid (label / value) so the value is
                always right-aligned with `text-right` and `break-all`
                allows long ISO timestamps + " · 3h ago" or a long
                task_id to wrap cleanly instead of overflowing the modal
                at narrow widths.
              */}
              <div className="border-t border-border pt-4 space-y-1 text-xs text-muted-foreground">
                <div className="grid grid-cols-[6rem_1fr] gap-2">
                  <span>Created</span>
                  <span className="font-mono text-xs break-all text-right" title={task.created_at}>
                    {task.created_at} · {formatRelative(task.created_at)}
                  </span>
                </div>
                <div className="grid grid-cols-[6rem_1fr] gap-2">
                  <span>Updated</span>
                  <span className="font-mono text-xs break-all text-right" title={task.updated_at}>
                    {task.updated_at} · {formatRelative(task.updated_at)}
                  </span>
                </div>
                <div className="grid grid-cols-[6rem_1fr] gap-2">
                  <span>Task ID</span>
                  <span className="font-mono text-xs break-all text-right">{task.task_id}</span>
                </div>
              </div>
            </div>

            <DialogFooter className="px-6 py-4 border-t border-border flex-shrink-0">
              {onEdit && (
                <Button variant="outline" size="sm" onClick={onEdit}>
                  <Pencil className="h-4 w-4 mr-1" />
                  Edit
                </Button>
              )}
              {onDelete && (
                <Button variant="destructive" size="sm" onClick={onDelete}>
                  <Trash2 className="h-4 w-4 mr-1" />
                  Delete
                </Button>
              )}
              <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
})
ViewTaskDialog.displayName = 'ViewTaskDialog'

// ---------- Edit dialog -------------------------------------------

interface EditTaskDialogProps extends RowDialogProps {
  onSaved: () => void
}

const EditTaskDialog = React.memo(({ task, onOpenChange, onSaved }: EditTaskDialogProps) => {
  const open = task !== null

  const [editTitle, setEditTitle] = useState('')
  const [editDescription, setEditDescription] = useState('')
  const [editStatus, setEditStatus] = useState<Task['status'] | 'unassigned'>('pending')
  const [editPriority, setEditPriority] = useState<Task['priority']>('medium')
  // AgentSelect speaks `string | null` directly; null = unassigned.
  // Pre-PR this was the string sentinel `__unassigned__` because the
  // local <Select> couldn't use an empty value — that hack moved
  // inside <AgentSelect>'s NONE_SENTINEL plumbing.
  const [editAssignedTo, setEditAssignedTo] = useState<string | null>(null)
  // New-note textarea is append-only: the backend stores notes as a
  // JSON array and `/api/update-task-dashboard` appends a single
  // entry per request. Empty string = no note added. Cleared on save.
  const [editNote, setEditNote] = useState<string>('')
  const [saving, setSaving] = useState(false)

  // Existing notes for the "Existing notes" preview block at the
  // bottom of the Edit dialog. Read-only here — to edit historical
  // notes you'd need per-note IDs which don't exist in the schema.
  const existingNotes = task ? (parseJsonField(task.notes) as Array<{ author: string; timestamp: string; content: string }>) : []

  // Re-seed form whenever the dialog opens for a *different* task.
  // Note: with live-lookup useDialog (Candidate D, 2026-06-02) the
  // `task` prop reference can change on every background refresh
  // even when the underlying fields are unchanged — keying the effect
  // on task identity prevents the refresh from blowing away the
  // admin's in-progress edits. Only the New-note textarea is reset
  // between opens; existing field edits survive. (The save error used
  // to be reset here too — it is now a shared toast, which owns its
  // own lifetime.)
  const taskId = task?.task_id
  useEffect(() => {
    if (!task) return
    setEditTitle(task.title || '')
    setEditDescription(task.description || '')
    setEditStatus(task.status || 'pending')
    setEditPriority(task.priority || 'medium')
    setEditAssignedTo(task.assigned_to || null)
    setEditNote('')
    // We deliberately depend on taskId, not the whole task object —
    // see the comment above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId])

  // Pre-PR: this dialog fetched its own agent list via the unfiltered
  // apiClient.getAgents() endpoint, which returns every row including
  // status='terminated' — leaking ghost agents into the Assigned-to
  // dropdown. Replaced 2026-06-04 by the shared <AgentSelect>, which
  // reads from data-store::getActiveAgents() (live-only). No local
  // fetch needed.

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!task) return
    setSaving(true)
    try {
      // Build the patch: include every editable field so the admin
      // can blanket-overwrite, but normalise assigned_to so the
      // sentinel becomes a real null.
      const patch: Record<string, unknown> = {
        title: editTitle,
        description: editDescription,
        status: editStatus as Task['status'],
        priority: editPriority,
        // AgentSelect speaks string|null directly — pass it through.
        assigned_to: editAssignedTo,
      }
      // Append-only: only include `notes` in the patch when the new-note
      // textarea has content. The backend treats `notes: str` as "append
      // a new entry with author=admin + timestamp"; passing empty would
      // be a no-op but we omit it to keep the request body minimal.
      const trimmedNote = editNote.trim()
      if (trimmedNote) patch.notes = trimmedNote
      await apiClient.updateTask(task.task_id, patch)
      onSaved()
      onOpenChange(false)
    } catch (err) {
      // Was a bespoke inline `saveError` banner (architecture review
      // Class 1: three error idioms for one failure mode). The shared
      // toast owns mutation errors now — the dialog stays open with
      // the operator's edits intact because we never reach the
      // onSaved()/close pair above.
      toastError(err, 'Failed to save task')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-xl bg-card border-border text-card-foreground p-0 gap-0">
        {task && (
          <>
            <DialogHeader className="px-6 pt-6 pb-4 border-b border-border">
              <DialogTitle className="text-lg">Edit task</DialogTitle>
              <DialogDescription className="text-muted-foreground">
                Changes are saved via POST /api/update-task-dashboard.
              </DialogDescription>
            </DialogHeader>
            <form onSubmit={handleSave}>
              {/* Scrollable body — only this region overflows. */}
              <div className="px-6 py-4 max-h-[80vh] overflow-y-auto space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="edit-task-title" className="text-sm text-muted-foreground">Title</Label>
                  <Input
                    id="edit-task-title"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    required
                    className="w-full bg-background border-border text-foreground"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="edit-task-description" className="text-sm text-muted-foreground">Description</Label>
                  <Textarea
                    id="edit-task-description"
                    value={editDescription}
                    onChange={(e) => setEditDescription(e.target.value)}
                    className="w-full bg-background border-border text-foreground min-h-[100px] whitespace-pre-wrap font-mono text-xs"
                  />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="edit-task-status" className="text-sm text-muted-foreground">Status</Label>
                    <Select value={editStatus} onValueChange={(v) => setEditStatus(v as Task['status'])}>
                      <SelectTrigger id="edit-task-status" className="w-full bg-background border-border text-foreground">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="bg-background border-border">
                        <SelectItem value="pending">pending</SelectItem>
                        <SelectItem value="in_progress">in_progress</SelectItem>
                        <SelectItem value="completed">completed</SelectItem>
                        <SelectItem value="cancelled">cancelled</SelectItem>
                        <SelectItem value="failed">failed</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="edit-task-priority" className="text-sm text-muted-foreground">Priority</Label>
                    <Select value={editPriority} onValueChange={(v) => setEditPriority(v as Task['priority'])}>
                      <SelectTrigger id="edit-task-priority" className="w-full bg-background border-border text-foreground">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="bg-background border-border">
                        <SelectItem value="low">low</SelectItem>
                        <SelectItem value="medium">medium</SelectItem>
                        <SelectItem value="high">high</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="edit-task-assigned" className="text-sm text-muted-foreground">Assigned to</Label>
                  {/*
                    Migrated 2026-06-04 (feat/agent-select-dropdown):
                    previously a local <Select> populated by fetching
                    the unfiltered apiClient.getAgents endpoint — which
                    returns every row including status='terminated',
                    leaking ghost agents into the dropdown. Now uses
                    the shared <AgentSelect> backed by
                    data-store::getActiveAgents (live-only).
                    noneLabel="— Unassigned —" matches CreateTaskModal
                    so both forms speak the same nullable-assignment
                    language. Pre-PR sentinel `__unassigned__` is now
                    an internal detail of <AgentSelect>.
                  */}
                  <AgentSelect
                    id="edit-task-assigned"
                    value={editAssignedTo}
                    onChange={setEditAssignedTo}
                    noneLabel="— Unassigned —"
                  />
                </div>
                {/*
                  Add-note section. Append-only — the backend appends a
                  new {timestamp, author, content} entry to the JSON
                  notes array; we cannot edit/delete historical notes
                  per-id (no PK in the schema). Leaving the textarea
                  empty skips the notes field in the patch. The
                  existing-notes preview below is read-only and gives
                  the admin context for the new note they're typing.
                */}
                <div className="border-t border-border pt-4 space-y-2">
                  <Label htmlFor="edit-task-note" className="text-sm text-muted-foreground">
                    Add note
                  </Label>
                  <Textarea
                    id="edit-task-note"
                    value={editNote}
                    onChange={(e) => setEditNote(e.target.value)}
                    placeholder="Optional. Appended to the task notes log with your admin id and a timestamp."
                    className="w-full bg-background border-border text-foreground min-h-[60px] whitespace-pre-wrap text-sm"
                  />
                  {existingNotes.length > 0 && (
                    <details className="text-xs text-muted-foreground">
                      <summary className="cursor-pointer hover:text-foreground">
                        Existing notes ({existingNotes.length})
                      </summary>
                      <div className="mt-2 space-y-2 max-h-[20vh] overflow-y-auto">
                        {existingNotes.map((note, idx: number) => (
                          <div key={idx} className="bg-muted/40 rounded p-2">
                            <div className="flex items-center justify-between mb-1">
                              <span className={cn(
                                "font-medium",
                                isTombstone(note.author) && "italic"
                              )}>
                                {note.author || 'unknown'}
                              </span>
                              <span title={note.timestamp}>
                                {formatRelative(note.timestamp)}
                              </span>
                            </div>
                            <p className="whitespace-pre-wrap text-foreground">{note.content}</p>
                          </div>
                        ))}
                      </div>
                    </details>
                  )}
                </div>
                {/* Monospace task_id footer — matches the View dialog idiom. */}
                <div className="border-t border-border pt-3 flex justify-between gap-2 text-xs text-muted-foreground">
                  <span>Task ID</span>
                  <span className="font-mono text-xs break-all">{task.task_id}</span>
                </div>
              </div>
              <DialogFooter className="px-6 py-4 border-t border-border gap-2">
                <Button type="button" variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={saving}>
                  Cancel
                </Button>
                <Button type="submit" size="sm" className="bg-primary hover:bg-primary/90 text-primary-foreground" disabled={saving}>
                  {saving ? 'Saving…' : 'Save'}
                </Button>
              </DialogFooter>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
})
EditTaskDialog.displayName = 'EditTaskDialog'

// ---------- Delete confirm dialog ---------------------------------
//
// EXTRACTED to `tasks/delete-task-dialog.tsx`. The hand-rolled copy that
// lived here was the third instance of the same {busy, error} +
// Cancel/destructive-confirm state machine (Schedules and Terminate were
// the others) — that duplication is now the shared tier-1
// <ConfirmActionModal>. The extraction also gives the dialog a test seam,
// which it needs because its confirmation tier is now CONDITIONAL: a leaf
// task keeps the one-click confirm, a task with descendants escalates to
// type-DELETE and shows what the cascade will take.


export function TasksDashboard() {
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  // Filter state — owned by useFilters (PR 4 of the 2026-06-09
  // architecture review). Two classes of filter live here:
  //
  //   * Client-side (title/description text search + priority): the
  //     rows are already in memory, so these narrow the list locally
  //     via the memoised `filteredTasks` pass below. Priority is NOT
  //     part of the GET /tasks contract, so it stays client-side.
  //
  //   * Server-side (status / assignment / creator): these drive the
  //     GET /tasks query params — the single source of truth shared
  //     with the backend + the MCP view_tasks tool. They are lifted
  //     into `serverFilters` (a TaskFilters) and threaded into the
  //     fetch, so the server does the filtering (and the "incomplete"
  //     status alias, which can't be expressed by matching a single
  //     stored status, resolves server-side).
  //
  // `clearAll` backs the "Clear" affordance; `isActive` toggles it.
  const { filters, setFilter, clearAll, isActive } = useFilters<{
    searchTerm: string
    priorityFilter: string
    statusFilter: string
    assignment: string
    createdBy: string
  }>({
    initial: {
      searchTerm: '',
      priorityFilter: 'all',
      statusFilter: 'all',
      assignment: 'all',
      createdBy: '',
    },
  })
  const { searchTerm, priorityFilter, statusFilter, assignment, createdBy } = filters

  // Lift the server-side dimensions into the TaskFilters shape the
  // getTasks() query-string builder consumes. Assignment uses the
  // dedicated `assigned` / `unassigned` booleans — never a magic
  // assigned_to="unassigned" value — so an agent named "unassigned"
  // can't collide with the claimable-pool filter.
  const serverFilters = useMemo<TaskFilters>(() => {
    const f: TaskFilters = {}
    if (statusFilter !== 'all') f.status = statusFilter
    if (assignment === 'assigned') f.assigned = true
    else if (assignment === 'unassigned') f.unassigned = true
    const cb = createdBy.trim()
    if (cb) f.created_by = cb
    return f
  }, [statusFilter, assignment, createdBy])

  const { tasks, loading, error, refresh, lastFetch, isConnected } = useTasksData(serverFilters)
  // Row-action dialog state. Each holds the **task_id** of the task
  // being viewed / edited / deleted via the live-lookup useDialog<T>
  // hook (Candidate D, architecture review 2026-06-02). The dialog
  // body reads `dialog.data` which is recomputed on every render by
  // the selector below — so background refresh, edits saved from the
  // sibling Edit dialog, and tombstoning the row all flow through
  // immediately. PR #74's Add-Note "saved note disappears" symptom
  // was the snapshot-mode bug this replaces. The legacy
  // TaskDetailsPanel sidebar has been retired — clicking a row body
  // now opens the View dialog (same as the eye icon).
  const taskSelector = useCallback(
    (id: string | null) => (id ? tasks.find(t => t.task_id === id) ?? null : null),
    [tasks],
  )
  const viewDialog = useDialog<Task>(taskSelector)
  const editDialog = useDialog<Task>(taskSelector)
  const deleteDialog = useDialog<Task>(taskSelector)

  // Deleted-while-open: if the row vanishes from the source under us
  // (background refresh sees a delete from another tab, or a sibling
  // dialog deletes the same task), the live selector returns null.
  // Auto-close the dialog so the user isn't stuck staring at an
  // empty modal; the row's gone, no point keeping the modal up.
  //
  // exhaustive-deps disabled for this block: useDialog returns a fresh
  // object each render, so we depend on its stable fields
  // (.isOpen/.data/.close) rather than the whole object. Listing the
  // object would re-run every render with no behavioural gain.
  /* eslint-disable react-hooks/exhaustive-deps */
  useEffect(() => {
    if (viewDialog.isOpen && viewDialog.data === null) viewDialog.close()
  }, [viewDialog.isOpen, viewDialog.data, viewDialog.close])
  useEffect(() => {
    if (editDialog.isOpen && editDialog.data === null) editDialog.close()
  }, [editDialog.isOpen, editDialog.data, editDialog.close])
  useEffect(() => {
    if (deleteDialog.isOpen && deleteDialog.data === null) deleteDialog.close()
  }, [deleteDialog.isOpen, deleteDialog.data, deleteDialog.close])
  /* eslint-enable react-hooks/exhaustive-deps */

  // Client-side narrowing only. Status + assignment + creator are
  // already applied server-side (see `serverFilters`), so `tasks` is
  // the pre-filtered set; here we only apply the in-memory text search
  // and the priority filter (priority isn't part of the GET /tasks
  // contract).
  const filteredTasks = useMemo(() => {
    return tasks.filter(task => {
      const matchesSearch = task.title.toLowerCase().includes(searchTerm.toLowerCase()) ||
                           (task.description && task.description.toLowerCase().includes(searchTerm.toLowerCase()))
      const matchesPriority = priorityFilter === 'all' || task.priority === priorityFilter
      return matchesSearch && matchesPriority
    })
  }, [tasks, searchTerm, priorityFilter])

  // PF-1 clamp — bound the rendered list to one PAGE_SIZE window. The
  // whole task set is already in memory (`filteredTasks`); this only
  // caps how many rows reach the DOM at once.
  const [currentOffset, setCurrentOffset] = useState(0)

  // Reset to the first page whenever the filtered set changes (a new
  // search/priority narrowing, or a server refetch shrinks the list) so
  // the offset can't strand the user past the end on an empty page.
  useEffect(() => {
    setCurrentOffset(0)
  }, [searchTerm, priorityFilter, statusFilter, assignment, createdBy])

  const totalFiltered = filteredTasks.length
  // Clamp the offset defensively in case the list shrank between renders
  // before the reset effect runs.
  const safeOffset =
    currentOffset >= totalFiltered
      ? Math.max(0, Math.floor(Math.max(0, totalFiltered - 1) / PAGE_SIZE) * PAGE_SIZE)
      : currentOffset
  const pagedTasks = useMemo(
    () => filteredTasks.slice(safeOffset, safeOffset + PAGE_SIZE),
    [filteredTasks, safeOffset],
  )

  const onFirstPage = safeOffset === 0
  const onLastPage = safeOffset + PAGE_SIZE >= totalFiltered
  const rangeStart = totalFiltered === 0 ? 0 : safeOffset + 1
  const rangeEnd = Math.min(safeOffset + PAGE_SIZE, totalFiltered)
  const goNewest = () => setCurrentOffset(0)
  const goNewer = () => setCurrentOffset(Math.max(0, safeOffset - PAGE_SIZE))
  const goOlder = () => setCurrentOffset(safeOffset + PAGE_SIZE)
  const goOldest = () =>
    setCurrentOffset(
      Math.floor(Math.max(0, totalFiltered - 1) / PAGE_SIZE) * PAGE_SIZE,
    )

  // Memoize stats calculation
  const stats = useMemo(() => {
    const total = tasks.length
    const in_progress = tasks.filter(t => t.status === 'in_progress').length
    const pending = tasks.filter(t => t.status === 'pending').length
    const completed = tasks.filter(t => t.status === 'completed').length
    const failed = tasks.filter(t => t.status === 'failed').length
    // Any status without a dedicated card (unassigned, cancelled, …) is in
    // `total` but not in the four cards; surface the remainder on the Total
    // card so the numbers reconcile
    // (total = in_progress + pending + completed + failed + other).
    const other = Math.max(0, total - in_progress - pending - completed - failed)
    return { total, in_progress, pending, completed, failed, other }
  }, [tasks])

  const handleCreateTask = useCallback(async (data: Parameters<typeof apiClient.createTask>[0]) => {
    try {
      await apiClient.createTask(data)
      // Refresh tasks after creating a new one
      refresh()
    } catch (err) {
      // Was a silent `console.error` (architecture review Class 1: the
      // operator saw the modal close and nothing appear). Mutation
      // errors go through the shared toast now.
      toastError(err, 'Failed to create task')
    }
  }, [refresh])

  // Row-action handlers. Each opens the matching Dialog. openView
  // is used by BOTH the eye icon and the row-body click — the
  // sidebar (TaskDetailsPanel) is retired. We forward the stable
  // hook .open methods directly.
  const openView = viewDialog.open
  const openEdit = editDialog.open
  const openDelete = deleteDialog.open
  const handleEditSaved = useCallback(() => { refresh() }, [refresh])
  const handleDeleted = useCallback(() => { refresh() }, [refresh])

  // Stats strip — rendered by the scaffold's <StatsCard> row. Note the
  // down-trend tint is the shared `text-destructive`; this page's
  // private copy used `text-orange-500`, the outlier of the four
  // drifted StatsCard copies (architecture review Class 6).
  const statsCards: StatsCardProps[] = [
    {
      icon: Target,
      label: 'Total',
      value: stats.total,
      change:
        stats.other > 0
          ? `${stats.other} other`
          : stats.total > 0 ? `${stats.in_progress} active` : undefined,
      trend: 'neutral',
    },
    {
      icon: Zap,
      label: 'Active',
      value: stats.in_progress,
      change: stats.total > 0 ? `${Math.round((stats.in_progress / stats.total) * 100)}%` : '0%',
      trend: 'up',
    },
    {
      icon: Clock,
      label: 'Pending',
      value: stats.pending,
      change: stats.pending > 0 ? 'Queued' : 'None',
      trend: 'neutral',
    },
    {
      icon: CheckCircle2,
      label: 'Completed',
      value: stats.completed,
      change: stats.total > 0 ? `${Math.round((stats.completed / stats.total) * 100)}% done` : '0%',
      trend: 'up',
    },
    {
      icon: AlertCircle,
      label: 'Failed',
      value: stats.failed,
      change: stats.failed > 0 ? 'Need review' : 'All good',
      trend: stats.failed > 0 ? 'down' : 'neutral',
    },
  ]

  // Column spec — one source for the desktop table (via
  // <ResponsiveDataTable>) and the mobile twin. Cells reproduce the
  // pre-scaffold <CompactTaskRow> exactly.
  const columns: Column<Task>[] = useMemo(() => [
    {
      id: 'task',
      header: 'Task',
      cellClassName: 'py-3',
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
      id: 'status',
      header: 'Status',
      cellClassName: 'py-3',
      cell: (task) => (
        <Badge
          variant="outline"
          className={cn(
            "text-xs font-semibold border-0 px-3 py-1.5 rounded-md",
            statusBadgeClass(task.status),
          )}
        >
          {task.status.replace('_', ' ').toUpperCase()}
        </Badge>
      ),
    },
    {
      id: 'details',
      header: 'Details',
      cellClassName: 'py-3 max-w-xs',
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
      id: 'priority',
      header: 'Priority',
      cellClassName: 'py-3',
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
      id: 'relations',
      header: 'Relations',
      cellClassName: 'py-3',
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
      id: 'updated',
      header: 'Updated',
      cellClassName: 'py-3 text-xs text-muted-foreground font-mono',
      cell: (task) => <TaskUpdatedCell value={task.updated_at} />,
    },
    {
      id: 'actions',
      header: 'Actions',
      headClassName: 'w-24',
      cellClassName: 'py-3',
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

  /*
    Controls — un-boxed filter row matching Messages/Memories (no Card
    wrapper). Search + Priority narrow the in-memory rows client-side;
    Status + Assignment + Created-by drive the server-side GET /tasks
    query (single source of truth).

    The inner flex wrapper keeps this page's `sm:flex-wrap` — six
    controls don't fit one desktop row, and the scaffold's filter-bar
    slot is a plain non-wrapping row (right for the 1-2 control pages).
  */
  const filterBar = (
    <div className="flex flex-col sm:flex-row sm:flex-wrap items-stretch sm:items-center gap-2 sm:gap-3 w-full">
      <div className="relative flex-1 sm:max-w-xs">
        {/* CC-1/CC-13 audit 2026-06-02: Search + Select migrated to
            shadcn semantic tokens. Dropped hand-rolled focus:ring-
            teal-* (different color + uses :focus not :focus-visible).
            Now relies on the Input/Select primitives' built-in
            focus-visible:ring-ring/50 styles. */}
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Search tasks..."
          value={searchTerm}
          onChange={(e) => setFilter("searchTerm", e.target.value)}
          className="pl-10"
        />
      </div>
      {/* Status (server-side). "Incomplete (open)" sends
          status=incomplete — the backend alias for every non-terminal
          task (pending + in_progress). */}
      <Select value={statusFilter} onValueChange={(v) => setFilter("statusFilter", v)}>
        <SelectTrigger className="w-full sm:w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Any status</SelectItem>
          <SelectItem value="incomplete">Incomplete (open)</SelectItem>
          <SelectItem value="pending">Pending</SelectItem>
          <SelectItem value="in_progress">In Progress</SelectItem>
          <SelectItem value="completed">Completed</SelectItem>
          <SelectItem value="cancelled">Cancelled</SelectItem>
          <SelectItem value="failed">Failed</SelectItem>
        </SelectContent>
      </Select>
      {/* Assignment (server-side). Maps to the dedicated
          assigned=true / unassigned=true booleans — never a magic
          assigned_to="unassigned" value. */}
      <Select value={assignment} onValueChange={(v) => setFilter("assignment", v)}>
        <SelectTrigger className="w-full sm:w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Any assignment</SelectItem>
          <SelectItem value="assigned">Assigned</SelectItem>
          <SelectItem value="unassigned">Unassigned</SelectItem>
        </SelectContent>
      </Select>
      {/* Created by (server-side) → created_by. Live-agents picker
          (shared <AgentSelect>); noneLabel="— Any —" means no filter. */}
      <div className="w-full sm:w-44">
        <AgentSelect
          value={createdBy || null}
          onChange={(v) => setFilter("createdBy", v ?? "")}
          noneLabel="— Any —"
          placeholder="created by"
        />
      </div>
      {/* Priority (client-side — not part of the GET /tasks contract). */}
      <Select value={priorityFilter} onValueChange={(v) => setFilter("priorityFilter", v)}>
        <SelectTrigger className="w-full sm:w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Any priority</SelectItem>
          <SelectItem value="high">High</SelectItem>
          <SelectItem value="medium">Medium</SelectItem>
          <SelectItem value="low">Low</SelectItem>
        </SelectContent>
      </Select>
      {isActive && (
        <Button variant="ghost" size="sm" onClick={clearAll}>
          <X className="h-4 w-4 mr-1" />
          Clear
        </Button>
      )}
    </div>
  )

  return (
    <DataTablePage<Task>
      guard={
        !isConnected
          ? {
              icon: CheckSquare,
              title: 'No Server Connection',
              description: 'Connect to an MCP server to manage tasks',
            }
          : null
      }
      loading={loading}
      error={error}
      header={{
        title: 'Task Operations',
        subtitle: 'Orchestrate and monitor autonomous tasks',
        serverName: activeServer?.name,
        lastUpdated: lastFetch > 0 ? lastFetch : undefined,
        onRefresh: refresh,
        refreshing: loading,
        actions: <CreateTaskModal onCreateTask={handleCreateTask} />,
      }}
      stats={statsCards}
      filterBar={filterBar}
      columns={columns}
      rows={pagedTasks}
      getRowId={(task) => task.task_id}
      onRowClick={(task) => openView(task.task_id)}
      renderMobileCard={(task) => (
        <TaskMobileCard
          task={task}
          openView={openView}
          openEdit={openEdit}
          openDelete={openDelete}
        />
      )}
      skeletonRows={6}
      empty={{
        icon: CheckSquare,
        title: 'No tasks found',
        description:
          tasks.length === 0
            ? "Create your first task to get started."
            : "No tasks match your current filters.",
        action:
          tasks.length === 0
            ? <CreateTaskModal onCreateTask={handleCreateTask} />
            : undefined,
      }}
    >
      {/* PF-1 clamp footer — only when the list spills past one page.
          Renders directly beneath the table (DataTablePage drops its
          children there). */}
      {totalFiltered > PAGE_SIZE && (
        <div className="mt-4">
          <TasksPagination
            rangeStart={rangeStart}
            rangeEnd={rangeEnd}
            total={totalFiltered}
            onFirstPage={onFirstPage}
            onLastPage={onLastPage}
            onNewest={goNewest}
            onNewer={goNewer}
            onOlder={goOlder}
            onOldest={goOldest}
          />
        </div>
      )}

      {/* Row-action dialogs (View / Edit / Delete) — Dialog modals,
          NOT the sidebar Sheet. Mirrors the messages-tab popup
          pattern from PR #36. */}
      <ViewTaskDialog
        task={viewDialog.data}
        onOpenChange={(open) => { if (!open) viewDialog.close() }}
        onEdit={() => {
          const task = viewDialog.data
          if (!task) return
          viewDialog.close()
          openEdit(task.task_id)
        }}
        onDelete={() => {
          const task = viewDialog.data
          if (!task) return
          viewDialog.close()
          openDelete(task.task_id)
        }}
      />
      <EditTaskDialog
        task={editDialog.data}
        onOpenChange={(open) => { if (!open) editDialog.close() }}
        onSaved={handleEditSaved}
      />
      <DeleteTaskDialog
        task={deleteDialog.data}
        onOpenChange={(open) => { if (!open) deleteDialog.close() }}
        onDeleted={handleDeleted}
      />
    </DataTablePage>
  )
}
