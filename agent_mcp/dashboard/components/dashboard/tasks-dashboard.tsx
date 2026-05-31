"use client"

import React, { useState, useEffect, useCallback, useMemo } from "react"
import {
  CheckSquare, Clock, AlertCircle, Users,
  Search, Plus, Eye, Pencil, Trash2,
  ArrowUp, ArrowDown, Minus, CheckCircle2, Target, Zap, GitBranch, RefreshCw
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { apiClient, Task, Agent } from "@/lib/api"
import { useServerStore } from "@/lib/stores/server-store"
import { cn } from "@/lib/utils"

// Status / priority colour helpers shared by the row + the View / Edit
// modals. Keep these here so the styles match the rest of the page.
const statusBadgeClass = (status: Task['status']): string => {
  const map: Record<string, string> = {
    in_progress: "bg-teal-500/15 text-teal-400 dark:text-teal-300 ring-1 ring-teal-500/20",
    pending: "bg-amber-500/15 text-amber-500 dark:text-amber-300 ring-1 ring-amber-500/20",
    completed: "bg-emerald-500/15 text-emerald-500 dark:text-emerald-300 ring-1 ring-emerald-500/20",
    cancelled: "bg-slate-500/15 text-slate-500 dark:text-slate-300 ring-1 ring-slate-500/20",
    failed: "bg-orange-500/15 text-orange-500 dark:text-orange-300 ring-1 ring-orange-500/20",
  }
  return map[status] || map.pending
}

const priorityBadgeClass = (priority: Task['priority']): string => {
  const map: Record<string, string> = {
    high: "bg-orange-500/10 text-orange-500 dark:text-orange-300 border-orange-500/20",
    medium: "bg-amber-500/10 text-amber-500 dark:text-amber-300 border-amber-500/20",
    low: "bg-slate-500/10 text-slate-500 dark:text-slate-300 border-slate-500/20",
  }
  return map[priority] || map.medium
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

const formatRelative = (iso: string | undefined): string => {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const diff = Date.now() - t
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return `${Math.floor(diff / 86_400_000)}d ago`
}

// Cache for tasks data
const tasksCache = new Map<string, { data: Task[], timestamp: number }>()
const CACHE_DURATION = 30000 // 30 seconds
const REFRESH_INTERVAL = 60000 // 1 minute for background refresh

// Real data hook for tasks with caching
const useTasksData = () => {
  const { activeServerId, servers } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  
  const [tasks, setTasks] = useState<Task[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastFetch, setLastFetch] = useState<number>(0)

  const fetchData = useCallback(async (forceRefresh = false) => {
    if (!activeServerId || !activeServer || activeServer.status !== 'connected') {
      setTasks([])
      setError(null)
      setLoading(false)
      return
    }

    const cacheKey = `${activeServerId}-tasks`
    const now = Date.now()
    const cached = tasksCache.get(cacheKey)

    // Use cache if it's fresh and not forcing refresh
    if (!forceRefresh && cached && (now - cached.timestamp) < CACHE_DURATION) {
      setTasks(cached.data)
      setError(null)
      setLoading(false)
      return
    }

    // Only show loading on first fetch or force refresh
    if (tasks.length === 0 || forceRefresh) {
      setLoading(true)
    }

    try {
      const tasksData = await apiClient.getTasks()
      
      // Update cache
      tasksCache.set(cacheKey, { data: tasksData, timestamp: now })
      
      setTasks(tasksData)
      setError(null)
      setLastFetch(now)
    } catch (err) {
      // Don't set error state for no server connection - that's handled by DashboardWrapper
      if (err instanceof Error && err.message !== 'NO_SERVER_CONNECTED') {
        setError(err.message)
        console.error('Error fetching tasks:', err)
      }
    } finally {
      setLoading(false)
    }
  }, [activeServerId, activeServer, tasks.length])

  useEffect(() => {
    fetchData()

    // Background refresh - less frequent and doesn't show loading
    const interval = setInterval(() => {
      fetchData(false)
    }, REFRESH_INTERVAL)

    return () => clearInterval(interval)
  }, [fetchData])

  // Manual refresh function
  const refresh = useCallback(() => fetchData(true), [fetchData])

  // Memoize return value to prevent unnecessary re-renders
  return useMemo(() => ({
    tasks, 
    loading, 
    error, 
    refresh,
    lastFetch,
    isConnected: !!activeServerId && activeServer?.status === 'connected' 
  }), [tasks, loading, error, refresh, lastFetch, activeServerId, activeServer])
}

const StatusDot = React.memo(({ status }: { status: Task['status'] }) => {
  const config = {
    in_progress: "bg-teal-400 shadow-teal-400/50 shadow-md animate-pulse",
    pending: "bg-amber-400 shadow-amber-400/50 shadow-md",
    completed: "bg-emerald-400 shadow-emerald-400/50 shadow-md",
    cancelled: "bg-slate-500 shadow-slate-500/50 shadow-md",
    failed: "bg-orange-400 shadow-orange-400/50 shadow-md animate-pulse",
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
    high: { icon: ArrowUp, className: "text-orange-400" },
    medium: { icon: Minus, className: "text-amber-400" },
    low: { icon: ArrowDown, className: "text-slate-400" },
  }
  
  const configItem = config[priority] || config.medium // fallback to medium if priority is undefined
  const { icon: Icon, className } = configItem
  return <Icon className={cn("h-4 w-4", className)} />
})
PriorityIcon.displayName = 'PriorityIcon'

interface CompactTaskRowProps {
  task: Task
  // Three distinct row-action handlers. Each opens its own Dialog
  // modal (View / Edit / Delete). The icon buttons stopPropagation so
  // the row-level click handler (which now opens the same View
  // dialog) doesn't fire twice when the icons are pressed.
  openView: (task: Task) => void
  openEdit: (task: Task) => void
  openDelete: (task: Task) => void
}

const CompactTaskRow = React.memo(({ task, openView, openEdit, openDelete }: CompactTaskRowProps) => {
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  const formatDate = useCallback((dateString: string) => {
    if (!mounted) return "..."
    return new Date(dateString).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    })
  }, [mounted])

  return (
    <TableRow className="border-teal-500/10 dark:border-teal-500/10 border-teal-600/20 hover:bg-teal-500/5 dark:hover:bg-teal-500/5 hover:bg-teal-600/10 group transition-all duration-200 cursor-pointer" onClick={() => openView(task)}>
      <TableCell className="py-3">
        <div className="flex items-center gap-3">
          <StatusDot status={task.status} />
          <PriorityIcon priority={task.priority} />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-sm text-foreground truncate">{task.title}</div>
            <div className="text-xs text-muted-foreground font-mono">#{task.task_id.slice(-6)}</div>
          </div>
        </div>
      </TableCell>
      
      <TableCell className="py-3">
        <Badge 
          variant="outline" 
          className={cn(
            "text-xs font-semibold border-0 px-3 py-1.5 rounded-md",
            task.status === 'in_progress' && "bg-teal-500/15 text-teal-400 dark:text-teal-300 ring-1 ring-teal-500/20",
            task.status === 'pending' && "bg-amber-500/15 text-amber-500 dark:text-amber-300 ring-1 ring-amber-500/20",
            task.status === 'completed' && "bg-emerald-500/15 text-emerald-500 dark:text-emerald-300 ring-1 ring-emerald-500/20",
            task.status === 'cancelled' && "bg-slate-500/15 text-slate-500 dark:text-slate-300 ring-1 ring-slate-500/20",
            task.status === 'failed' && "bg-orange-500/15 text-orange-500 dark:text-orange-300 ring-1 ring-orange-500/20"
          )}
        >
          {task.status.replace('_', ' ').toUpperCase()}
        </Badge>
      </TableCell>
      
      <TableCell className="py-3 max-w-xs">
        <div className="text-sm text-foreground truncate">
          {task.description || "No description"}
        </div>
        {task.assigned_to && (
          <div className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
            <Users className="h-3 w-3" />
            {task.assigned_to}
          </div>
        )}
      </TableCell>
      
      <TableCell className="py-3">
        <Badge 
          variant="outline" 
          className={cn(
            "text-xs font-medium px-2 py-0.5",
            task.priority === 'high' && "bg-orange-500/10 text-orange-500 dark:text-orange-300 border-orange-500/20",
            task.priority === 'medium' && "bg-amber-500/10 text-amber-500 dark:text-amber-300 border-amber-500/20",
            task.priority === 'low' && "bg-slate-500/10 text-slate-500 dark:text-slate-300 border-slate-500/20"
          )}
        >
          {task.priority.toUpperCase()}
        </Badge>
      </TableCell>
      
      <TableCell className="py-3">
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
      </TableCell>
      
      <TableCell className="py-3 text-xs text-muted-foreground font-mono">
        {formatDate(task.updated_at)}
      </TableCell>
      
      <TableCell className="py-3">
        {/*
          Three distinct row icons. Each opens its own Dialog modal
          (View / Edit / Delete). stopPropagation prevents the row-
          level onClick (which opens the same View dialog) from
          firing twice when the eye icon is pressed and from opening
          the View dialog when Edit/Delete are pressed.
        */}
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Button
            variant="ghost"
            size="sm"
            title="View task"
            aria-label="View task"
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
            onClick={(e) => { e.stopPropagation(); openView(task) }}
          >
            <Eye className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            title="Edit task"
            aria-label="Edit task"
            className="h-7 w-7 p-0 text-teal-400 hover:text-teal-300 hover:bg-teal-500/10"
            onClick={(e) => { e.stopPropagation(); openEdit(task) }}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            title="Delete task"
            aria-label="Delete task"
            className="h-7 w-7 p-0 text-destructive hover:text-destructive hover:bg-destructive/10"
            onClick={(e) => { e.stopPropagation(); openDelete(task) }}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </TableCell>
    </TableRow>
  )
}, (prevProps, nextProps) => {
  // Only re-render if the task actually changed
  return JSON.stringify(prevProps.task) === JSON.stringify(nextProps.task)
})
CompactTaskRow.displayName = 'CompactTaskRow'

const StatsCard = React.memo(({ icon: Icon, label, value, change, trend }: {
  icon: any
  label: string
  value: number
  change?: string
  trend?: 'up' | 'down' | 'neutral'
}) => (
  <div className="bg-slate-900/60 dark:bg-slate-900/60 bg-white/80 border border-slate-800/60 dark:border-slate-800/60 border-slate-200 rounded-xl p-[var(--space-fluid-md)] backdrop-blur-sm hover:bg-slate-900/80 dark:hover:bg-slate-900/80 hover:bg-white/90 transition-all duration-200 group">
    <div className="flex items-center justify-between">
      <div>
        <div className="flex items-center gap-2 mb-2">
          <Icon className="h-4 w-4 text-slate-400 dark:text-slate-400 text-slate-600 group-hover:text-slate-300 dark:group-hover:text-slate-300 group-hover:text-slate-700 transition-colors" />
          <span className="text-fluid-xs font-semibold text-slate-400 dark:text-slate-400 text-slate-600 uppercase tracking-wider">{label}</span>
        </div>
        <div className="text-fluid-2xl font-bold text-white dark:text-white text-slate-900 mb-1">{value}</div>
        {change && (
          <div className={cn(
            "text-fluid-xs font-medium",
            trend === 'up' && "text-teal-400",
            trend === 'down' && "text-orange-400",
            trend === 'neutral' && "text-slate-400"
          )}>
            {change}
          </div>
        )}
      </div>
    </div>
  </div>
))
StatsCard.displayName = 'StatsCard'

const CreateTaskModal = React.memo(({ onCreateTask }: { onCreateTask: (data: any) => void }) => {
  const [open, setOpen] = useState(false)
  const [formData, setFormData] = useState({
    title: '',
    description: '',
    priority: 'medium' as Task['priority'],
    assigned_to: ''
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!formData.title.trim()) return

    onCreateTask({
      title: formData.title.trim(),
      description: formData.description.trim() || undefined,
      priority: formData.priority,
      assigned_to: formData.assigned_to.trim() || undefined
    })

    setFormData({ title: '', description: '', priority: 'medium', assigned_to: '' })
    setOpen(false)
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" className="bg-teal-600 hover:bg-teal-700 text-white shadow-lg hover:shadow-teal-500/25 transition-all duration-200">
          <Plus className="h-4 w-4 mr-1.5" />
          Create Task
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md bg-card border-border text-card-foreground">
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
              <Input
                value={formData.assigned_to}
                onChange={(e) => setFormData(prev => ({ ...prev, assigned_to: e.target.value }))}
                placeholder="agent-01"
                className="bg-background border-border text-foreground font-mono text-sm"
              />
            </div>
          </div>
          <DialogFooter className="gap-2">
            <Button type="button" variant="outline" onClick={() => setOpen(false)} size="sm">
              Cancel
            </Button>
            <Button type="submit" size="sm" className="bg-teal-600 hover:bg-teal-700 shadow-lg hover:shadow-teal-500/25 transition-all">
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

const ViewTaskDialog = React.memo(({ task, onOpenChange }: RowDialogProps) => {
  const open = task !== null

  // Parse JSON-shaped optional fields safely.
  const dependencies = task ? parseJsonField(task.depends_on_tasks) : []
  const childTasks = task ? parseJsonField(task.child_tasks) : []
  const notes = task ? parseJsonField(task.notes) : []
  const createdBy: string | undefined = task ? (task as unknown as { created_by?: string }).created_by : undefined

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl bg-card border-border text-card-foreground p-0 gap-0">
        {task && (
          <>
            <DialogHeader className="px-6 pt-6 pb-4 border-b border-border">
              <DialogTitle className="flex items-center justify-between pr-8 gap-2">
                <span className="text-lg font-semibold truncate">{task.title}</span>
                <div className="flex items-center gap-2 flex-shrink-0">
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

            {/* Scrollable body: only this section overflows, not the
                whole modal (per shadcn idiom). */}
            <div className="px-6 py-4 max-h-[80vh] overflow-y-auto space-y-4 text-sm">
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

              {/* Group 2: description — separated by a subtle divider */}
              <div className="border-t border-border pt-4 space-y-2">
                <Label className="text-xs text-muted-foreground uppercase tracking-wider">Description</Label>
                <p className="text-sm whitespace-pre-wrap break-words font-mono text-xs bg-muted/40 rounded p-3">
                  {task.description || <span className="text-muted-foreground italic font-sans">(no description)</span>}
                </p>
              </div>

              {/* Group 3: relations (only renders if present) */}
              {(dependencies.length > 0 || childTasks.length > 0) && (
                <div className="border-t border-border pt-4 space-y-4">
                  {dependencies.length > 0 && (
                    <div className="space-y-2">
                      <Label className="text-xs text-muted-foreground uppercase tracking-wider">Depends on</Label>
                      <div className="flex flex-wrap gap-2">
                        {dependencies.map((id: any, idx) => (
                          <Badge key={idx} variant="outline" className="text-xs font-mono">{String(id)}</Badge>
                        ))}
                      </div>
                    </div>
                  )}
                  {childTasks.length > 0 && (
                    <div className="space-y-2">
                      <Label className="text-xs text-muted-foreground uppercase tracking-wider">Subtasks</Label>
                      <div className="flex flex-wrap gap-2">
                        {childTasks.map((id: any, idx) => (
                          <Badge key={idx} variant="outline" className="text-xs font-mono">{String(id)}</Badge>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Group 4: notes (only renders if present) */}
              {notes.length > 0 && (
                <div className="border-t border-border pt-4 space-y-2">
                  <Label className="text-xs text-muted-foreground uppercase tracking-wider">Notes ({notes.length})</Label>
                  <div className="space-y-2">
                    {notes.map((note: any, idx) => (
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
                </div>
              )}

              {/* Group 5: tombstone metadata footer in monospace */}
              <div className="border-t border-border pt-4 space-y-1 text-xs text-muted-foreground">
                <div className="flex justify-between gap-2">
                  <span>Created</span>
                  <span className="font-mono text-xs" title={task.created_at}>
                    {task.created_at} · {formatRelative(task.created_at)}
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>Updated</span>
                  <span className="font-mono text-xs" title={task.updated_at}>
                    {task.updated_at} · {formatRelative(task.updated_at)}
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>Task ID</span>
                  <span className="font-mono text-xs break-all">{task.task_id}</span>
                </div>
              </div>
            </div>

            <DialogFooter className="px-6 py-4 border-t border-border">
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
  // Sentinel '__unassigned__' so the shadcn Select can represent NULL.
  const [editAssignedTo, setEditAssignedTo] = useState<string>('__unassigned__')
  const [agents, setAgents] = useState<Agent[]>([])
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  // Re-seed form whenever the dialog opens for a new task.
  useEffect(() => {
    if (!task) return
    setEditTitle(task.title || '')
    setEditDescription(task.description || '')
    setEditStatus(task.status || 'pending')
    setEditPriority(task.priority || 'medium')
    setEditAssignedTo(task.assigned_to || '__unassigned__')
    setSaveError(null)
  }, [task])

  // Fetch agents for the Assigned-to dropdown. Best-effort: if it
  // fails, the dropdown is empty (admin can still unassign).
  useEffect(() => {
    if (!open) return
    apiClient.getAgents().then(setAgents).catch(() => setAgents([]))
  }, [open])

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!task) return
    setSaving(true)
    setSaveError(null)
    try {
      // Build the patch: include every editable field so the admin
      // can blanket-overwrite, but normalise assigned_to so the
      // sentinel becomes a real null.
      const patch: Record<string, unknown> = {
        title: editTitle,
        description: editDescription,
        status: editStatus as Task['status'],
        priority: editPriority,
        assigned_to: editAssignedTo === '__unassigned__' ? null : editAssignedTo,
      }
      await apiClient.updateTask(task.task_id, patch)
      onSaved()
      onOpenChange(false)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl bg-card border-border text-card-foreground p-0 gap-0">
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
                  <Select value={editAssignedTo} onValueChange={setEditAssignedTo}>
                    <SelectTrigger id="edit-task-assigned" className="w-full bg-background border-border text-foreground">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-background border-border">
                      <SelectItem value="__unassigned__">(unassigned)</SelectItem>
                      {agents.map((a) => (
                        <SelectItem key={a.agent_id} value={a.agent_id}>{a.agent_id}</SelectItem>
                      ))}
                      {/* If the current value isn't in the agent list, keep it as an explicit option. */}
                      {editAssignedTo !== '__unassigned__' && !agents.find((a) => a.agent_id === editAssignedTo) && (
                        <SelectItem value={editAssignedTo}>{editAssignedTo} (stale)</SelectItem>
                      )}
                    </SelectContent>
                  </Select>
                </div>
                {saveError && (
                  <div className="text-xs text-destructive bg-destructive/10 border border-destructive/30 rounded p-2">
                    {saveError}
                  </div>
                )}
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
                <Button type="submit" size="sm" className="bg-teal-600 hover:bg-teal-700" disabled={saving}>
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

interface DeleteTaskDialogProps extends RowDialogProps {
  onDeleted: () => void
}

const DeleteTaskDialog = React.memo(({ task, onOpenChange, onDeleted }: DeleteTaskDialogProps) => {
  const open = task !== null
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) setError(null)
  }, [open])

  const handleConfirm = async () => {
    if (!task) return
    setBusy(true)
    setError(null)
    try {
      await apiClient.deleteTask(task.task_id)
      onDeleted()
      onOpenChange(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md bg-card border-border text-card-foreground p-0 gap-0">
        {task && (
          <>
            <DialogHeader className="px-6 pt-6 pb-4">
              <DialogTitle className="text-lg">Delete task</DialogTitle>
              <DialogDescription className="text-muted-foreground">
                Delete task &ldquo;{task.title}&rdquo;? This cannot be undone.
              </DialogDescription>
            </DialogHeader>
            <div className="px-6 pb-4 max-h-[80vh] overflow-y-auto space-y-3">
              <div className="space-y-2">
                <Label className="text-sm text-muted-foreground">Task ID</Label>
                <div className="text-xs text-muted-foreground font-mono break-all border border-border rounded p-2 bg-muted/30">
                  {task.task_id}
                </div>
              </div>
              {error && (
                <div className="text-xs text-destructive bg-destructive/10 border border-destructive/30 rounded p-2">
                  {error}
                </div>
              )}
            </div>
            <DialogFooter className="px-6 py-4 border-t border-border gap-2">
              <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleConfirm}
                disabled={busy}
              >
                <Trash2 className="h-3.5 w-3.5 mr-1.5" />
                {busy ? 'Deleting…' : 'Delete'}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
})
DeleteTaskDialog.displayName = 'DeleteTaskDialog'

export function TasksDashboard() {
  const { tasks, loading, error, refresh, lastFetch, isConnected } = useTasksData()
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  const [searchTerm, setSearchTerm] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [priorityFilter, setPriorityFilter] = useState<string>('all')
  // Row-action dialog state. Each holds the task being viewed / edited
  // / deleted, or null when the dialog is closed. The legacy
  // TaskDetailsPanel sidebar has been retired — clicking a row body
  // now opens the View dialog (same as the eye icon).
  const [viewTask, setViewTask] = useState<Task | null>(null)
  const [editTask, setEditTask] = useState<Task | null>(null)
  const [deleteTask, setDeleteTask] = useState<Task | null>(null)

  // Memoize filtered tasks to prevent unnecessary recalculations
  const filteredTasks = useMemo(() => {
    return tasks.filter(task => {
      const matchesSearch = task.title.toLowerCase().includes(searchTerm.toLowerCase()) ||
                           (task.description && task.description.toLowerCase().includes(searchTerm.toLowerCase()))
      const matchesStatus = statusFilter === 'all' || task.status === statusFilter
      const matchesPriority = priorityFilter === 'all' || task.priority === priorityFilter
      return matchesSearch && matchesStatus && matchesPriority
    })
  }, [tasks, searchTerm, statusFilter, priorityFilter])

  // Memoize stats calculation
  const stats = useMemo(() => ({
    total: tasks.length,
    in_progress: tasks.filter(t => t.status === 'in_progress').length,
    pending: tasks.filter(t => t.status === 'pending').length,
    completed: tasks.filter(t => t.status === 'completed').length,
    failed: tasks.filter(t => t.status === 'failed').length,
  }), [tasks])

  const handleCreateTask = useCallback(async (data: any) => {
    try {
      await apiClient.createTask(data)
      // Refresh tasks after creating a new one
      refresh()
    } catch (error) {
      console.error('Failed to create task:', error)
    }
  }, [refresh])

  // Row-action handlers. Each opens the matching Dialog. The
  // onOpenChange callback that the Dialog itself passes back uses
  // null to mean "close". openView is used by BOTH the eye icon and
  // the row-body click — the sidebar (TaskDetailsPanel) is retired.
  const openView = useCallback((task: Task) => setViewTask(task), [])
  const openEdit = useCallback((task: Task) => setEditTask(task), [])
  const openDelete = useCallback((task: Task) => setDeleteTask(task), [])
  const handleEditSaved = useCallback(() => { refresh() }, [refresh])
  const handleDeleted = useCallback(() => { refresh() }, [refresh])

  if (!isConnected) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-4">
          <CheckSquare className="h-12 w-12 text-muted-foreground mx-auto" />
          <div>
            <h3 className="text-lg font-medium text-foreground mb-2">No Server Connection</h3>
            <p className="text-muted-foreground text-sm">Connect to an MCP server to manage tasks</p>
          </div>
        </div>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-4">
          <div className="animate-spin h-8 w-8 border-2 border-primary border-t-transparent rounded-full mx-auto" />
          <p className="text-muted-foreground text-sm">Loading tasks...</p>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-4">
          <AlertCircle className="h-12 w-12 text-destructive mx-auto" />
          <div>
            <h3 className="text-lg font-medium text-foreground mb-2">Connection Error</h3>
            <p className="text-destructive text-sm">{error}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="relative w-full space-y-[var(--space-fluid-lg)]">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-fluid-2xl font-bold text-white dark:text-white text-slate-900">Task Operations</h1>
          <p className="text-slate-400 dark:text-slate-400 text-slate-600 text-fluid-base mt-1">Orchestrate and monitor autonomous tasks</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          <Badge variant="outline" className="text-xs bg-teal-500/15 text-teal-300 border-teal-500/30 font-medium">
            <div className="w-2 h-2 bg-teal-400 rounded-full mr-2 animate-pulse" />
            {activeServer?.name}
          </Badge>
          {lastFetch > 0 && (
            <span className="text-xs text-muted-foreground">
              Last updated: {new Date(lastFetch).toLocaleTimeString()}
            </span>
          )}
          <Button 
            variant="outline" 
            size="sm" 
            onClick={refresh}
            disabled={loading}
            className="text-xs"
          >
            <RefreshCw className={cn("h-3.5 w-3.5 mr-1.5", loading && "animate-spin")} />
            Refresh
          </Button>
          <CreateTaskModal onCreateTask={handleCreateTask} />
        </div>
      </div>

      {/* Stats */}
      <div className="grid gap-[var(--space-fluid-md)] grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        <StatsCard 
          icon={Target} 
          label="Total" 
          value={stats.total} 
          change={stats.total > 0 ? `${stats.in_progress} active` : undefined}
          trend="neutral"
        />
        <StatsCard 
          icon={Zap} 
          label="Active" 
          value={stats.in_progress} 
          change={stats.total > 0 ? `${Math.round((stats.in_progress/stats.total)*100)}%` : "0%"}
          trend="up"
        />
        <StatsCard 
          icon={Clock} 
          label="Pending" 
          value={stats.pending} 
          change={stats.pending > 0 ? "Queued" : "None"}
          trend="neutral"
        />
        <StatsCard 
          icon={CheckCircle2} 
          label="Completed" 
          value={stats.completed} 
          change={stats.total > 0 ? `${Math.round((stats.completed/stats.total)*100)}% done` : "0%"}
          trend="up"
        />
        <StatsCard 
          icon={AlertCircle} 
          label="Failed" 
          value={stats.failed} 
          change={stats.failed > 0 ? "Need review" : "All good"}
          trend={stats.failed > 0 ? "down" : "neutral"}
        />
      </div>

      {/* Controls */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-[var(--space-fluid-sm)]">
        <div className="relative flex-1 sm:max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-teal-400 dark:text-teal-400 text-teal-600" />
          <Input
            placeholder="Search tasks..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="pl-10 bg-slate-900/60 dark:bg-slate-900/60 bg-white/80 border-slate-700/60 dark:border-slate-700/60 border-slate-300 text-white dark:text-white text-slate-900 placeholder:text-slate-400 focus:border-teal-500/50 focus:ring-teal-500/20 transition-all"
          />
        </div>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-full sm:w-36 bg-slate-900/60 dark:bg-slate-900/60 bg-white/80 border-slate-700/60 dark:border-slate-700/60 border-slate-300 text-white dark:text-white text-slate-900">
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="bg-slate-900 dark:bg-slate-900 bg-white border-slate-800 dark:border-slate-800 border-slate-200">
            <SelectItem value="all">All Status</SelectItem>
            <SelectItem value="pending">Pending</SelectItem>
            <SelectItem value="in_progress">In Progress</SelectItem>
            <SelectItem value="completed">Completed</SelectItem>
            <SelectItem value="failed">Failed</SelectItem>
            <SelectItem value="cancelled">Cancelled</SelectItem>
          </SelectContent>
        </Select>
        <Select value={priorityFilter} onValueChange={setPriorityFilter}>
          <SelectTrigger className="w-full sm:w-32 bg-slate-900/60 dark:bg-slate-900/60 bg-white/80 border-slate-700/60 dark:border-slate-700/60 border-slate-300 text-white dark:text-white text-slate-900">
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="bg-slate-900 dark:bg-slate-900 bg-white border-slate-800 dark:border-slate-800 border-slate-200">
            <SelectItem value="all">All Priority</SelectItem>
            <SelectItem value="high">High</SelectItem>
            <SelectItem value="medium">Medium</SelectItem>
            <SelectItem value="low">Low</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Tasks Table */}
      <div className="bg-slate-900/50 dark:bg-slate-900/50 bg-white/90 border border-teal-500/20 dark:border-teal-500/20 border-teal-600/30 rounded-xl overflow-x-auto backdrop-blur-md shadow-xl shadow-teal-500/10">
        <Table>
          <TableHeader>
            <TableRow className="border-teal-500/10 dark:border-teal-500/10 border-teal-600/20 hover:bg-transparent">
              <TableHead className="text-white/70 dark:text-white/70 text-slate-700 font-medium text-xs uppercase tracking-wider">Task</TableHead>
              <TableHead className="text-white/70 dark:text-white/70 text-slate-700 font-medium text-xs uppercase tracking-wider">Status</TableHead>
              <TableHead className="text-white/70 dark:text-white/70 text-slate-700 font-medium text-xs uppercase tracking-wider">Details</TableHead>
              <TableHead className="text-white/70 dark:text-white/70 text-slate-700 font-medium text-xs uppercase tracking-wider">Priority</TableHead>
              <TableHead className="text-white/70 dark:text-white/70 text-slate-700 font-medium text-xs uppercase tracking-wider">Relations</TableHead>
              <TableHead className="text-white/70 dark:text-white/70 text-slate-700 font-medium text-xs uppercase tracking-wider">Updated</TableHead>
              <TableHead className="text-white/70 dark:text-white/70 text-slate-700 font-medium text-xs uppercase tracking-wider w-24">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filteredTasks.map((task) => (
              <CompactTaskRow
                key={task.task_id}
                task={task}
                openView={openView}
                openEdit={openEdit}
                openDelete={openDelete}
              />
            ))}
          </TableBody>
        </Table>

        {filteredTasks.length === 0 && (
          <div className="p-12 text-center">
            <CheckSquare className="h-12 w-12 text-teal-400/50 dark:text-teal-400/50 text-teal-600/50 mx-auto mb-4" />
            <h3 className="text-lg font-medium text-white dark:text-white text-slate-900 mb-2">No tasks found</h3>
            <p className="text-white/60 dark:text-white/60 text-slate-600 text-sm mb-4">
              {tasks.length === 0 ? "Create your first task to get started" : "No tasks match your current filters"}
            </p>
            {tasks.length === 0 && <CreateTaskModal onCreateTask={handleCreateTask} />}
          </div>
        )}
      </div>

      {/* Row-action dialogs (View / Edit / Delete) — Dialog modals,
          NOT the sidebar Sheet. Mirrors the messages-tab popup
          pattern from PR #36. */}
      <ViewTaskDialog
        task={viewTask}
        onOpenChange={(open) => { if (!open) setViewTask(null) }}
      />
      <EditTaskDialog
        task={editTask}
        onOpenChange={(open) => { if (!open) setEditTask(null) }}
        onSaved={handleEditSaved}
      />
      <DeleteTaskDialog
        task={deleteTask}
        onOpenChange={(open) => { if (!open) setDeleteTask(null) }}
        onDeleted={handleDeleted}
      />
    </div>
  )
}