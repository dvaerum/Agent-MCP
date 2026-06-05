"use client"

import React, { useState, useEffect, useCallback } from "react"
import {
  Users, Clock, AlertCircle, CheckCircle2, Shield, Cpu, Database, Network, Terminal,
  Search, Plus, Eye, RefreshCw, Copy, RotateCcw, Trash2, Pencil
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { apiClient, Agent, Task } from "@/lib/api"
import { projectContext } from "@/lib/project-context"
import { mcpUrl } from "@/lib/urls"
import { useServerStore } from "@/lib/stores/server-store"
import { useDataStore } from "@/lib/stores/data-store"
import { cn } from "@/lib/utils"
import { useDialog } from "@/hooks/use-dialog"
import { TaskDetailsDialog } from "./task-details-dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/dashboard/shared/empty-state"
import { AgentsMobileList } from "@/components/dashboard/agents-mobile-list"


const StatusDot = React.memo(({ status }: { status: Agent['status'] }) => {
  const config = {
    running: "bg-primary shadow-primary/50 shadow-md",
    pending: "bg-warning shadow-warning/50 shadow-md animate-pulse",
    terminated: "bg-muted-foreground shadow-muted-foreground/50 shadow-md",
    failed: "bg-destructive shadow-destructive/50 shadow-md animate-pulse",
  }
  
  return (
    <div className={cn(
      "w-2.5 h-2.5 rounded-full",
      config[status] || config.pending
    )} />
  )
})

const AgentTypeIcon = React.memo(({ agentId }: { agentId: string }) => {
  const getIcon = () => {
    if (agentId.includes('admin')) return Shield
    if (agentId.includes('worker')) return Cpu
    if (agentId.includes('analysis')) return Database
    if (agentId.includes('security')) return Shield
    return Terminal
  }
  
  const Icon = getIcon()
  return <Icon className="h-4 w-4 text-muted-foreground" />
})

const CompactAgentRow = React.memo(({ agent, onTerminate, onRestore, onPurge, openView, onEdit, onTaskClick }: {
  agent: Agent,
  onTerminate: (id: string) => void,
  onRestore: (id: string) => void,
  onPurge: (id: string) => void,
  openView: (agent: Agent) => void,
  onEdit: (agent: Agent) => void,
  onTaskClick: (task: Task) => void
}) => {
  const { getAgentTasks } = useDataStore()
  
  // Check if agent is new (less than 10 minutes old)
  const isNewAgent = () => {
    if (agent.agent_id === 'Admin' || agent.created_at === 'N/A') return false
    const now = new Date()
    const createdAt = new Date(agent.created_at)
    const ageInMinutes = (now.getTime() - createdAt.getTime()) / (1000 * 60)
    return ageInMinutes <= 10 && !agent.current_task
  }
  
  // Get agent's tasks and recent actions
  const agentTasks = getAgentTasks(agent.agent_id)
  const currentTask = agentTasks.find(t => t.task_id === agent.current_task)
  
  // Calculate task stats - separate assigned vs worked on  
  // Use the data store's logic for consistent ID matching
  const cleanAgentId = agent.agent_id.startsWith('agent_') ? agent.agent_id.substring(6) : agent.agent_id
  const normalizedAgentId = cleanAgentId === 'Admin' ? 'admin' : cleanAgentId
  
  const assignedTasks = agentTasks.filter(t => 
    t.assigned_to === normalizedAgentId || 
    t.assigned_to === cleanAgentId ||
    (normalizedAgentId === 'admin' && (t.assigned_to === 'Admin' || t.assigned_to === 'admin'))
  )
  const workedOnTasks = agentTasks.filter(t => 
    t.assigned_to !== normalizedAgentId && 
    t.assigned_to !== cleanAgentId &&
    !(normalizedAgentId === 'admin' && (t.assigned_to === 'Admin' || t.assigned_to === 'admin'))
  )
  
  const taskStats = {
    total: agentTasks.length,
    assigned: assignedTasks.length,
    workedOn: workedOnTasks.length,
    pending: agentTasks.filter(t => t.status === 'pending').length,
    inProgress: agentTasks.filter(t => t.status === 'in_progress').length,
    completed: agentTasks.filter(t => t.status === 'completed').length
  }
  


  return (
    // Row body click opens the View dialog — mirrors the Tasks page
    // pattern (PR #54). The action buttons inside this row each
    // stopPropagation so their clicks don't bubble up and re-trigger
    // openView on top of (e.g.) a terminate confirm.
    <TableRow
      className="border-border/50 hover:bg-muted/30 group transition-all duration-200 cursor-pointer"
      onClick={() => openView(agent)}
    >
      <TableCell className="py-3">
        <div className="flex items-center gap-3">
          <StatusDot status={agent.status} />
          <AgentTypeIcon agentId={agent.agent_id} />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-sm text-foreground truncate">{agent.agent_id}</div>
            <div className="text-xs text-muted-foreground font-mono">#{agent.agent_id.slice(-6)}</div>
          </div>
        </div>
      </TableCell>
      
      <TableCell className="py-3">
        <div className="flex items-center gap-2">
          <Badge 
            variant="outline" 
            className={cn(
              "text-xs font-semibold border-0 px-3 py-1.5 rounded-md",
              agent.status === 'running' && "bg-primary/15 text-primary ring-1 ring-primary/20",
              agent.status === 'pending' && "bg-warning/15 text-warning ring-1 ring-warning/20",
              agent.status === 'terminated' && "bg-muted/50 text-muted-foreground ring-1 ring-border",
              agent.status === 'failed' && "bg-destructive/15 text-destructive ring-1 ring-destructive/20"
            )}
          >
            {agent.status.toUpperCase()}
          </Badge>
          {isNewAgent() && (
            <Badge variant="outline" className="text-xs bg-blue-500/15 text-blue-600 border-blue-500/30 font-medium">
              NEW
            </Badge>
          )}
          {/* Event-coord PR-3: in-flight wait_for_events indicator.
              `wait_for_events_in_flight` is sourced from /api/all-data,
              which snapshots `g.lock_for(agent_id).locked()` server-side
              (the PR-2 per-agent serialization lock). Hidden when
              FALSE / absent so the row stays uncluttered when the
              agent isn't auto-looping. Distinct sky-blue palette so it
              reads as "status decoration" rather than the primary
              running/pending/terminated/failed status. */}
          {agent.wait_for_events_in_flight && (
            <Badge
              variant="outline"
              className="text-xs bg-sky-500/15 text-sky-600 border-sky-500/30 font-medium"
              title="Agent is in a wait_for_events long-poll (auto event-loop)"
            >
              WAITING
            </Badge>
          )}
        </div>
      </TableCell>
      
      <TableCell className="py-3 max-w-xs">
        {currentTask ? (
          <div>
            <button
              onClick={(e) => { e.stopPropagation(); onTaskClick(currentTask) }}
              className="text-sm text-foreground hover:text-primary truncate block text-left hover:underline"
            >
              {currentTask.title}
            </button>
            <div className="text-xs text-muted-foreground mt-1">
              {taskStats.assigned > 0 && `${taskStats.assigned} assigned`}
              {taskStats.assigned > 0 && taskStats.workedOn > 0 && ', '}
              {taskStats.workedOn > 0 && `${taskStats.workedOn} contributed`}
              {taskStats.total === 0 && 'No tasks'}
            </div>
          </div>
        ) : (
          <div>
            <div className="text-sm text-muted-foreground truncate">No active task</div>
            {taskStats.total > 0 && (
              <div className="text-xs text-muted-foreground mt-1">
                {taskStats.assigned > 0 && `${taskStats.assigned} assigned`}
                {taskStats.assigned > 0 && taskStats.workedOn > 0 && ', '}
                {taskStats.workedOn > 0 && `${taskStats.workedOn} contributed`}
              </div>
            )}
          </div>
        )}
      </TableCell>
      
      <TableCell className="py-3">
        {agent.auth_token ? (
          <div className="flex items-center gap-2">
            <code className="text-xs font-mono text-muted-foreground max-w-[120px] truncate">
              {agent.auth_token.slice(0, 8)}...
            </code>
            <Button
              variant="ghost"
              size="sm"
              onClick={(e) => {
                e.stopPropagation()
                navigator.clipboard.writeText(agent.auth_token || '')
                // You could add a toast notification here
              }}
              className="h-6 w-6 p-0"
            >
              <Copy className="h-3 w-3" />
            </Button>
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">No token</span>
        )}
      </TableCell>

      {/* Row-action buttons. Every onClick must stopPropagation —
          otherwise the row-body onClick (which opens View) fires on
          top of the destructive Terminate / Purge confirm. */}
      <TableCell className="py-3">
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); openView(agent) }}
            title="View details"
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
          >
            <Eye className="h-3.5 w-3.5" />
          </Button>
          {agent.agent_id !== 'Admin' && (
            <Button
              variant="ghost"
              size="sm"
              onClick={(e) => { e.stopPropagation(); onEdit(agent) }}
              title="Edit agent"
              className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
            >
              <Pencil className="h-3.5 w-3.5" />
            </Button>
          )}
          {agent.status !== 'terminated' && agent.agent_id !== 'Admin' && (
            <Button
              variant="ghost"
              size="sm"
              onClick={(e) => { e.stopPropagation(); onTerminate(agent.agent_id) }}
              title="Terminate (soft-delete; can be restored or purged after)"
              className="h-7 w-7 p-0 text-destructive hover:text-destructive/80 hover:bg-destructive/10"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          )}
          {agent.status === 'terminated' && agent.agent_id !== 'Admin' && (
            <>
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => { e.stopPropagation(); onRestore(agent.agent_id) }}
                title="Restore"
                className="h-7 px-2 text-xs text-primary hover:text-primary/80 hover:bg-primary/10"
              >
                <RotateCcw className="h-3.5 w-3.5 mr-1" />
                Restore
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => { e.stopPropagation(); onPurge(agent.agent_id) }}
                title="Purge"
                className="h-7 px-2 text-xs text-destructive hover:text-destructive/80 hover:bg-destructive/10"
              >
                <Trash2 className="h-3.5 w-3.5 mr-1" />
                Purge
              </Button>
            </>
          )}
        </div>
      </TableCell>
    </TableRow>
  )
})

const StatsCard = ({ icon: Icon, label, value, change, trend }: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: number
  change?: string
  trend?: 'up' | 'down' | 'neutral'
}) => (
  // CC-4/CC-8/CC-16 audit 2026-06-02: rounded-lg + plain Tailwind
  // sizing + tabular-nums on numerals.
  <div className="bg-card border border-border rounded-lg p-3 sm:p-5 hover:bg-muted/30 transition-colors duration-150 group">
    <div className="flex items-center justify-between">
      <div>
        <div className="flex items-center gap-2 mb-2">
          <Icon className="h-4 w-4 text-muted-foreground transition-colors" />
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">{label}</span>
        </div>
        <div className="text-2xl sm:text-3xl font-semibold text-foreground tabular-nums mb-1">{value}</div>
        {change && (
          <div className={cn(
            "text-xs font-medium tabular-nums",
            trend === 'up' && "text-emerald-500",
            trend === 'down' && "text-destructive",
            trend === 'neutral' && "text-muted-foreground"
          )}>
            {change}
          </div>
        )}
      </div>
    </div>
  </div>
)

interface CreateAgentData {
  agent_id: string;
  capabilities?: string[];
  working_directory?: string;
}

const CreateAgentModal = ({ onCreateAgent }: { onCreateAgent: (data: CreateAgentData) => void }) => {
  const [open, setOpen] = useState(false)
  const [formData, setFormData] = useState({
    agent_id: '',
    capabilities: '',
    working_directory: ''
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!formData.agent_id.trim()) return

    const capabilities = formData.capabilities
      .split(',')
      .map(c => c.trim())
      .filter(c => c.length > 0)

    onCreateAgent({
      agent_id: formData.agent_id.trim(),
      capabilities: capabilities.length > 0 ? capabilities : undefined,
      working_directory: formData.working_directory.trim() || undefined
    })

    setFormData({ agent_id: '', capabilities: '', working_directory: '' })
    setOpen(false)
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" className="bg-primary hover:bg-primary/90 text-primary-foreground shadow-lg hover:shadow-primary/25 transition-all duration-200">
          <Plus className="h-4 w-4 mr-1.5" />
          Deploy
        </Button>
      </DialogTrigger>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-md bg-card border-border text-card-foreground">
        <DialogHeader>
          <DialogTitle className="text-lg">Deploy Agent</DialogTitle>
          <DialogDescription className="text-muted-foreground">
            Configure a new agent for deployment.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              Agent ID
            </label>
            <Input
              value={formData.agent_id}
              onChange={(e) => setFormData(prev => ({ ...prev, agent_id: e.target.value }))}
              placeholder="worker-analytics-01"
              className="bg-background border-border text-foreground"
              required
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              Capabilities
            </label>
            <Textarea
              value={formData.capabilities}
              onChange={(e) => setFormData(prev => ({ ...prev, capabilities: e.target.value }))}
              placeholder="data-analysis, file-ops, web-search"
              className="bg-background border-border text-foreground h-20 resize-none"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              Working Directory
            </label>
            <Input
              value={formData.working_directory}
              onChange={(e) => setFormData(prev => ({ ...prev, working_directory: e.target.value }))}
              placeholder="/workspace/analytics"
              className="bg-background border-border text-foreground font-mono text-sm"
            />
          </div>
          <DialogFooter className="gap-2">
            <Button type="button" variant="outline" onClick={() => setOpen(false)} size="sm">
              Cancel
            </Button>
            <Button type="submit" size="sm" className="bg-primary hover:bg-primary/90 shadow-lg hover:shadow-primary/25 transition-all">
              Deploy
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// Performance profiling callback
const onRender = (id: string, phase: "mount" | "update" | "nested-update", actualDuration: number, baseDuration: number, startTime: number, commitTime: number) => {
  if (process.env.NODE_ENV === 'development') {
    console.log(`[Profiler] ${id} ${phase}:`, {
      actualDuration: `${actualDuration.toFixed(2)}ms`,
      baseDuration: `${baseDuration.toFixed(2)}ms`,
      startTime: `${startTime.toFixed(2)}ms`,
      commitTime: `${commitTime.toFixed(2)}ms`
    })
  }
}

type PurgePreview = Awaited<ReturnType<typeof apiClient.getPurgePreview>>

const PurgeAgentDialog = ({
  agentId,
  open,
  onOpenChange,
  onConfirmed,
}: {
  agentId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirmed: () => void
}) => {
  const [preview, setPreview] = useState<PurgePreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [purging, setPurging] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !agentId) {
      setPreview(null)
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    apiClient
      .getPurgePreview(agentId)
      .then((p) => {
        if (!cancelled) setPreview(p)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, agentId])

  const handleConfirm = async () => {
    if (!agentId) return
    setPurging(true)
    setError(null)
    try {
      await apiClient.purgeAgent(agentId)
      onConfirmed()
      onOpenChange(false)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPurging(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-md bg-card border-border text-card-foreground">
        <DialogHeader>
          <DialogTitle className="text-lg">
            Purge agent {agentId ?? ''}?
          </DialogTitle>
          <DialogDescription className="text-muted-foreground">
            This deletes the agent row and tombstones every reference
            to <code>{agentId}</code> as
            <code className="ml-1">{preview?.tombstone ?? `[deleted-${agentId ?? ''}]`}</code>.
            Task notes are preserved as an audit trail.
          </DialogDescription>
        </DialogHeader>
        {loading && (
          <div className="text-sm text-muted-foreground py-4">
            Loading preview...
          </div>
        )}
        {error && (
          <div className="text-sm text-destructive py-2">{error}</div>
        )}
        {preview && (
          <div className="space-y-2 text-sm">
            <div className="font-medium">This will tombstone:</div>
            <ul className="list-disc pl-5 space-y-1 text-muted-foreground">
              <li>
                {preview.counts.messages_sent} messages sent
                {preview.samples.messages_sent[0] && (
                  <span className="text-xs block">
                    (last: &lsquo;{preview.samples.messages_sent[0].content}&rsquo;)
                  </span>
                )}
              </li>
              <li>{preview.counts.messages_received} messages received</li>
              <li>
                {preview.counts.tasks_created} tasks created
                {preview.samples.tasks_created[0] && (
                  <span className="text-xs block">
                    (e.g. &lsquo;{preview.samples.tasks_created[0]}&rsquo;)
                  </span>
                )}
              </li>
              <li>
                {preview.counts.tasks_assigned} tasks assigned
                {preview.counts.tasks_assigned > 0 && (
                  <span className="text-xs block">
                    (will be set to unassigned)
                  </span>
                )}
              </li>
              <li>{preview.counts.agent_actions} agent_actions entries</li>
            </ul>
          </div>
        )}
        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            size="sm"
            disabled={purging}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={handleConfirm}
            size="sm"
            disabled={purging || loading || !!error}
            className="bg-destructive hover:bg-destructive/90 text-destructive-foreground"
          >
            {purging ? 'Purging...' : 'Confirm purge'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Defensive normalisation. The backend column is JSON-encoded text but
// some endpoints (e.g. /api/all-data) return it parsed and others return
// the raw string. Without this guard, calling .join() on a string blows
// the whole agents page up with a TypeError.
const normalizeCapabilities = (caps: unknown): string[] => {
  if (Array.isArray(caps)) return caps.map((c) => String(c))
  if (typeof caps === 'string') {
    try {
      const parsed = JSON.parse(caps)
      if (Array.isArray(parsed)) return parsed.map((c) => String(c))
    } catch {
      // Fall through — treat as comma-separated.
    }
    return caps
      .split(',')
      .map((c) => c.trim())
      .filter((c) => c.length > 0)
  }
  return []
}

// Event-coord PR-1: SQLite BOOLEAN columns arrive as JS number (0/1)
// after the JSON round-trip; the JSON serializer never coerces them
// back to true/false. Default to TRUE when missing — matches the
// migration's `DEFAULT 1` backfill semantics.
const coerceAutoEventLoop = (raw: unknown): boolean => {
  if (typeof raw === 'boolean') return raw
  if (typeof raw === 'number') return raw !== 0
  if (typeof raw === 'string') {
    const s = raw.trim().toLowerCase()
    if (s === 'true' || s === '1') return true
    if (s === 'false' || s === '0') return false
  }
  return true
}

// Terminate confirmation. Soft-delete only — the row stays so the
// admin can hit Restore / Purge after.
const TerminateAgentDialog = ({
  agentId,
  open,
  onOpenChange,
  onConfirmed,
}: {
  agentId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirmed: (agentId: string) => Promise<void> | void
}) => {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleConfirm = async () => {
    if (!agentId) return
    setBusy(true)
    setError(null)
    try {
      await onConfirmed(agentId)
      onOpenChange(false)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-md bg-card border-border text-card-foreground">
        <DialogHeader>
          <DialogTitle className="text-lg">
            Terminate agent {agentId ?? ''}?
          </DialogTitle>
          <DialogDescription className="text-muted-foreground">
            This is a soft-delete. The agent&apos;s row, tasks, and messages
            are preserved — you can Restore it or Purge it (hard delete +
            tombstone) from the terminated-row actions after.
          </DialogDescription>
        </DialogHeader>
        {error && <div className="text-sm text-destructive py-2">{error}</div>}
        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={handleConfirm}
            disabled={busy}
            className="bg-destructive hover:bg-destructive/90 text-destructive-foreground"
          >
            {busy ? 'Terminating...' : 'Terminate'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Edit dialog — admin-editable agent fields. Backed by
// POST /api/agents/<id>/edit (capabilities / color / working_directory).
const EditAgentDialog = ({
  agent,
  open,
  onOpenChange,
  onSaved,
}: {
  agent: Agent | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved: () => void
}) => {
  const [capabilities, setCapabilities] = useState('')
  const [color, setColor] = useState('')
  const [workingDirectory, setWorkingDirectory] = useState('')
  const [aoeSessionId, setAoeSessionId] = useState('')
  // Event-coord PR-1: per-agent wake-loop toggle. Default true matches
  // the migration's DEFAULT 1 backfill.
  const [autoEventLoop, setAutoEventLoop] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Read the global event-loop flag from project_context so we can
  // disable + annotate the per-agent toggle when global is OFF (per
  // the locked-decisions table in the event-coord plan).
  const dataAll = useDataStore((s) => s.data)
  const globalEventLoop = React.useMemo<boolean>(() => {
    const row = dataAll?.context?.find(
      (c: any) => c.context_key === 'config_auto_event_loop_global'
    )
    if (!row) return true  // unset ⇒ default ON
    const raw = (row as any).value
    if (typeof raw === 'boolean') return raw
    if (typeof raw === 'string') {
      const s = raw.trim().toLowerCase()
      if (s === 'true') return true
      if (s === 'false') return false
      try {
        const parsed = JSON.parse(s)
        if (typeof parsed === 'boolean') return parsed
      } catch { /* fall through */ }
    }
    return true
  }, [dataAll])

  // Re-seed form whenever the dialog opens for a *different* agent.
  // With live-lookup useDialog (Candidate D, 2026-06-02) the agent
  // prop reference can change on every background refresh; keying the
  // effect on agent_id keeps the admin's in-progress field edits
  // alive instead of being clobbered by the latest store snapshot.
  const agentId = agent?.agent_id
  useEffect(() => {
    if (!open || !agent) return
    setCapabilities(normalizeCapabilities(agent.capabilities).join(', '))
    setColor(agent.color || '')
    setWorkingDirectory(agent.working_directory || '')
    setAoeSessionId(agent.aoe_session_id || '')
    // Event-coord PR-1: SQLite stores BOOLEAN as INTEGER 0/1, which
    // arrives as a JS number after the JSON round-trip. Coerce to
    // strict boolean and default to TRUE when the field is missing
    // (legacy backends).
    setAutoEventLoop(coerceAutoEventLoop(agent.auto_event_loop))
    setError(null)
    // Intentionally key on agentId, not the agent object — see above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, agentId])

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!agent) return
    setBusy(true)
    setError(null)
    const updates: {
      capabilities?: string[]
      color?: string
      working_directory?: string
      aoe_session_id?: string
      auto_event_loop?: boolean
    } = {}
    const parsedCaps = capabilities
      .split(',')
      .map((c) => c.trim())
      .filter((c) => c.length > 0)
    const currentCaps = normalizeCapabilities(agent.capabilities)
    if (JSON.stringify(parsedCaps) !== JSON.stringify(currentCaps)) {
      updates.capabilities = parsedCaps
    }
    if (color !== (agent.color || '')) {
      updates.color = color
    }
    if (workingDirectory !== (agent.working_directory || '')) {
      updates.working_directory = workingDirectory
    }
    const aoeTrimmed = aoeSessionId.trim().toLowerCase()
    if (aoeTrimmed !== (agent.aoe_session_id || '')) {
      // Client-side hint — the backend re-validates and 400s on bad input.
      if (aoeTrimmed && !/^[0-9a-f]{16}$/.test(aoeTrimmed)) {
        setError('AoE session id must be 16 lowercase hex chars (or empty to clear).')
        setBusy(false)
        return
      }
      updates.aoe_session_id = aoeTrimmed
    }
    // Event-coord PR-1: only send if changed from the agent's current
    // value (or from the default TRUE when the field is absent).
    const currentAutoEventLoop = coerceAutoEventLoop(agent.auto_event_loop)
    if (autoEventLoop !== currentAutoEventLoop) {
      updates.auto_event_loop = autoEventLoop
    }
    if (Object.keys(updates).length === 0) {
      onOpenChange(false)
      setBusy(false)
      return
    }
    try {
      await apiClient.editAgent(agent.agent_id, updates)
      onSaved()
      onOpenChange(false)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-md bg-card border-border text-card-foreground">
        <DialogHeader>
          <DialogTitle className="text-lg">Edit agent {agent?.agent_id}</DialogTitle>
          <DialogDescription className="text-muted-foreground">
            Update the agent&apos;s capabilities, color, or working directory.
            Status changes use Terminate / Restore / Purge.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSave} className="space-y-4">
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              Capabilities
            </label>
            <Textarea
              value={capabilities}
              onChange={(e) => setCapabilities(e.target.value)}
              placeholder="code_edit, file_read, web_search"
              className="bg-background border-border text-foreground h-20 resize-none"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              Color
            </label>
            <div className="flex items-center gap-2">
              <Input
                type="color"
                value={color || '#888888'}
                onChange={(e) => setColor(e.target.value)}
                className="h-9 w-12 p-1 bg-background border-border"
              />
              <Input
                value={color}
                onChange={(e) => setColor(e.target.value)}
                placeholder="#888888"
                className="bg-background border-border text-foreground font-mono"
              />
            </div>
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              Working Directory
            </label>
            <Input
              value={workingDirectory}
              onChange={(e) => setWorkingDirectory(e.target.value)}
              placeholder="/workspace/agent"
              className="bg-background border-border text-foreground font-mono text-sm"
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
              AoE Session ID
            </label>
            <Input
              value={aoeSessionId}
              onChange={(e) => setAoeSessionId(e.target.value)}
              placeholder="16-char lowercase hex, e.g. 551e7a79d11f435b"
              className="bg-background border-border text-foreground font-mono text-sm"
              maxLength={16}
              pattern="[0-9a-f]{16}"
            />
            <p className="text-[10px] text-muted-foreground mt-1">
              Binds this agent to a specific Agents-of-Empires tmux session for the
              notification side-channel. Leave empty to fall back to title-match.
            </p>
          </div>
          {/*
            Event-coord PR-1: per-agent wake-loop toggle. Default TRUE.
            Disabled (greyed) when the global flag is OFF — the
            wake-loop bootstrap requires BOTH flags ON per the
            locked-decisions table in the event-coord plan. Note text
            explicitly directs the operator to Settings to flip the
            global flag.
          */}
          <div className="rounded-md border border-border p-3 space-y-2">
            <div className="flex items-center justify-between gap-3">
              <div className="space-y-0.5">
                <label
                  htmlFor="agent-edit-auto-event-loop"
                  className="text-xs font-medium text-foreground uppercase tracking-wider"
                >
                  Auto event-loop
                </label>
                <p className="text-[11px] text-muted-foreground leading-snug">
                  When on (default), this agent receives the wake-loop
                  bootstrap and auto-calls wait_for_events on connect.
                  Both this toggle and the global Settings toggle must
                  be on.
                </p>
              </div>
              <Switch
                id="agent-edit-auto-event-loop"
                checked={autoEventLoop}
                onCheckedChange={setAutoEventLoop}
                disabled={!globalEventLoop || busy}
              />
            </div>
            {!globalEventLoop && (
              <p className="text-[11px] text-warning">
                Global event-loop is disabled — toggle it on in
                Settings to enable per-agent control.
              </p>
            )}
          </div>
          {error && <div className="text-sm text-destructive">{error}</div>}
          <DialogFooter className="gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onOpenChange(false)}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              size="sm"
              disabled={busy}
              className="bg-primary hover:bg-primary/90"
            >
              {busy ? 'Saving...' : 'Save changes'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ===== MCP-onboarding snippet helpers =====================================
//
// The View dialog grows a tabbed "add me as an MCP server" section.
// Each tab shows the copy-paste-ready config snippet for one MCP client
// targeting THIS specific agent. The server name is namespaced per
// agent_id (`agent-mcp-<agent_id>`) so multi-agent identity setups
// don't collide. URL is derived from the path-prefix adapter
// (lib/project-context.ts, PR #56). Transport is Streamable HTTP per
// MCP spec rev 2025-03-26 (PR #61) — POST/GET/DELETE on /mcp with
// Authorization: Bearer <agent_token>.
//
// Client schemas were verified against current docs (2026-06):
//   - OpenCode    https://opencode.ai/docs/mcp-servers
//   - Cursor      https://cursor.com/docs/context/mcp
//   - Cline       https://docs.cline.bot/mcp/configuring-mcp-servers
//   - Zed         https://zed.dev/docs/ai/mcp
//   - Continue    https://docs.continue.dev/customize/deep-dives/mcp
//                 (YAML format under .continue/mcpServers/<name>.yaml)
//   - Claude Code Anthropic CLI `claude mcp add --transport http`
//
// Generic JSON is a transport-agnostic fallback for clients we don't
// explicitly support.

type ClientTab =
  | 'claude-code'
  | 'opencode'
  | 'cursor'
  | 'cline'
  | 'zed'
  | 'continue'
  | 'generic'

const CLIENT_TABS: ReadonlyArray<{ value: ClientTab; label: string }> = [
  { value: 'claude-code', label: 'Claude Code' },
  { value: 'opencode', label: 'OpenCode' },
  { value: 'cursor', label: 'Cursor' },
  { value: 'cline', label: 'Cline' },
  { value: 'zed', label: 'Zed' },
  { value: 'continue', label: 'Continue.dev' },
  { value: 'generic', label: 'Generic JSON' },
]

const ACTIVE_TAB_STORAGE_KEY = 'agent-mcp-popup-active-client'

/**
 * Derive the public MCP endpoint URL for this dashboard's project.
 *
 * Under path-prefix deployments (the production shape) the dashboard
 * loads from `/agent-mcp/app/<name>/...` and the MCP transport lives
 * at `<origin>/agent-mcp/<name>/mcp` (PR-D will move it under the
 * top-level /mcp/ prefix). Standalone deployments (single-tenant, no
 * path prefix) expose the MCP transport at `<origin>/mcp` directly.
 *
 * Pre-PR-B this function had a bug (audit §1.1): it concatenated
 * `apiPrefix + "/mcp"`, which produced `/agent-mcp/__api/<name>/mcp`
 * — a path that doesn't exist (the MCP route is a sibling of __api,
 * not a child). PR-B routes the URL build through ``mcpUrl()`` in
 * ``lib/urls.ts`` so the next URL rename (PR-D) is a one-line change.
 */
function deriveMcpUrl(): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  if (projectContext.projectName) {
    return mcpUrl(projectContext.projectName, origin)
  }
  return `${origin}/mcp`
}

function buildSnippet(tab: ClientTab, agentId: string, token: string, url: string): string {
  const name = `agent-mcp-${agentId}`
  const tokenForSnippet = token || '<AGENT_TOKEN>'
  switch (tab) {
    case 'claude-code':
      return [
        '# 1. CLI — one-shot add via the Claude Code CLI:',
        `claude mcp add --transport http ${name} ${url} \\`,
        `  --header "Authorization: Bearer ${tokenForSnippet}"`,
        '',
        '# 2. Equivalent JSON (paste into ~/.claude.json under',
        '#    `mcpServers` for user-scope OR',
        '#    `projects["<cwd>"].mcpServers` for project-scope):',
        '"' + name + '": {',
        '  "type": "http",',
        `  "url": "${url}",`,
        `  "headers": {"Authorization": "Bearer ${tokenForSnippet}"}`,
        '}',
      ].join('\n')
    case 'opencode':
      // Verified against https://opencode.ai/docs/mcp-servers
      // Lives in opencode.json (project root) or
      // ~/.config/opencode/opencode.json (user-scope).
      return [
        '// opencode.json (project) — or ~/.config/opencode/opencode.json',
        '{',
        '  "$schema": "https://opencode.ai/config.json",',
        '  "mcp": {',
        `    "${name}": {`,
        '      "type": "remote",',
        `      "url": "${url}",`,
        '      "enabled": true,',
        `      "headers": {"Authorization": "Bearer ${tokenForSnippet}"}`,
        '    }',
        '  }',
        '}',
      ].join('\n')
    case 'cursor':
      // Verified against https://cursor.com/docs/context/mcp
      // Lives in .cursor/mcp.json (project) or ~/.cursor/mcp.json
      // (global).
      return [
        '// .cursor/mcp.json (project) — or ~/.cursor/mcp.json (global)',
        '{',
        '  "mcpServers": {',
        `    "${name}": {`,
        `      "url": "${url}",`,
        `      "headers": {"Authorization": "Bearer ${tokenForSnippet}"}`,
        '    }',
        '  }',
        '}',
      ].join('\n')
    case 'cline':
      // Verified against https://docs.cline.bot/mcp/configuring-mcp-servers
      // CLI: ~/.cline/mcp.json. IDE extensions: MCP Settings JSON
      // via the Configure tab.
      return [
        '// ~/.cline/mcp.json (CLI) — or the MCP Settings JSON in the',
        '// Configure tab for the VS Code / IDE extension',
        '{',
        '  "mcpServers": {',
        `    "${name}": {`,
        `      "url": "${url}",`,
        `      "headers": {"Authorization": "Bearer ${tokenForSnippet}"},`,
        '      "disabled": false,',
        '      "autoApprove": []',
        '    }',
        '  }',
        '}',
      ].join('\n')
    case 'zed':
      // Verified against https://zed.dev/docs/ai/mcp
      // Lives in ~/.config/zed/settings.json under `context_servers`.
      return [
        '// ~/.config/zed/settings.json — under context_servers',
        '{',
        '  "context_servers": {',
        `    "${name}": {`,
        `      "url": "${url}",`,
        `      "headers": {"Authorization": "Bearer ${tokenForSnippet}"}`,
        '    }',
        '  }',
        '}',
      ].join('\n')
    case 'continue':
      // Verified against https://docs.continue.dev/customize/deep-dives/mcp
      // Continue uses per-server YAML files under
      // `.continue/mcpServers/<server>.yaml`. The docs we could verify
      // do not show explicit Authorization-header syntax for HTTP
      // transports — the `requestOptions.headers` form below matches
      // the broader Continue config convention; verify against your
      // installed Continue version.
      return [
        '# .continue/mcpServers/' + name + '.yaml',
        '# NOTE: Authorization-header syntax for HTTP MCP servers in',
        '#       Continue is not explicitly documented in the public',
        '#       docs — verify against your installed version.',
        'mcpServers:',
        `  - name: ${name}`,
        '    type: streamable-http',
        `    url: ${url}`,
        '    requestOptions:',
        '      headers:',
        `        Authorization: "Bearer ${tokenForSnippet}"`,
      ].join('\n')
    case 'generic':
      return [
        '// Generic / transport-agnostic — adapt to your client\'s schema.',
        '{',
        `  "name": "${name}",`,
        `  "url": "${url}",`,
        '  "transport": "http",',
        `  "headers": {"Authorization": "Bearer ${tokenForSnippet}"}`,
        '}',
      ].join('\n')
  }
}

// SnippetBlock — a pre/code block with an inline Copy button. Pulled
// out of the tab body so each TabsContent stays a one-liner; also lets
// us count <Copy /> occurrences (one per tab + token copy + agent_id
// copy) cleanly.
const SnippetBlock = ({
  snippet,
  copied,
  onCopy,
}: {
  snippet: string
  copied: boolean
  onCopy: () => void
}) => (
  <div className="relative">
    <Button
      variant="ghost"
      size="sm"
      onClick={onCopy}
      className="absolute top-2 right-2 h-7 px-2 text-xs z-10"
      title="Copy snippet"
    >
      <Copy className="h-3 w-3 mr-1" />
      {copied ? 'Copied' : 'Copy'}
    </Button>
    <pre className="text-xs leading-relaxed font-mono bg-muted/40 rounded p-3 pr-20 whitespace-pre-wrap break-words [overflow-wrap:anywhere] max-h-[40vh] overflow-y-auto">
      {snippet}
    </pre>
  </div>
)

// Read-only agent details modal — replaces the old sidebar drawer.
// Uses the same Dialog primitive as the Messages row-detail popup
// (PR #36). Polished to match the Tasks page dialog idiom (PR #54):
// - sm:!max-w-3xl beats the base sm:max-w-lg
// - max-h-[90vh] + flex-col body with a single
//   flex-1 min-h-0 overflow-y-auto scroll region
// - sticky header / footer (flex-shrink-0)
// - long values use [overflow-wrap:anywhere] (32-hex tokens, snippets)
// - title uses line-clamp-3 break-words (NOT truncate)
//
// New: MCP-onboarding section — a Tabs primitive with one tab per
// supported client. Active tab persists to localStorage so a user's
// preferred client is sticky across sessions.
const AgentDetailDialog = ({
  agent,
  open,
  onOpenChange,
  onTaskClick,
}: {
  agent: Agent | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onTaskClick: (task: Task) => void
}) => {
  const [revealToken, setRevealToken] = useState(false)
  const [copied, setCopied] = useState(false)
  const [copiedSnippet, setCopiedSnippet] = useState<ClientTab | null>(null)
  const [activeTab, setActiveTab] = useState<ClientTab>('claude-code')
  const { getAgentTasks } = useDataStore()

  // Hydrate active tab from localStorage on first mount. We
  // deliberately seed lazily (inside useEffect, not in useState) so
  // SSR doesn't crash on `localStorage`.
  useEffect(() => {
    if (typeof window === 'undefined') return
    try {
      const stored = window.localStorage.getItem(ACTIVE_TAB_STORAGE_KEY)
      if (stored && CLIENT_TABS.some((t) => t.value === stored)) {
        setActiveTab(stored as ClientTab)
      }
    } catch {
      // localStorage can be disabled (private browsing); fall through.
    }
  }, [])

  useEffect(() => {
    if (!open) {
      setRevealToken(false)
      setCopied(false)
      setCopiedSnippet(null)
    }
  }, [open])

  if (!agent) return null

  const currentTask = getAgentTasks(agent.agent_id).find(
    (t) => t.task_id === agent.current_task,
  )

  const formatRelative = (iso: string) => {
    if (!iso || iso === 'N/A') return 'unknown'
    const d = new Date(iso)
    const diff = Date.now() - d.getTime()
    if (diff < 60_000) return 'just now'
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
    return `${Math.floor(diff / 86_400_000)}d ago`
  }

  const copyAgentId = () => {
    navigator.clipboard.writeText(agent.agent_id)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const mcpUrl = deriveMcpUrl()
  const snippetToken = agent.auth_token || ''

  const handleTabChange = (value: string) => {
    const next = value as ClientTab
    setActiveTab(next)
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.setItem(ACTIVE_TAB_STORAGE_KEY, next)
      } catch {
        // localStorage disabled — silently no-op.
      }
    }
  }

  const handleCopySnippet = (tab: ClientTab) => {
    const snippet = buildSnippet(tab, agent.agent_id, snippetToken, mcpUrl)
    navigator.clipboard.writeText(snippet)
    setCopiedSnippet(tab)
    setTimeout(() => setCopiedSnippet(null), 1500)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        Width: sm:!max-w-3xl (Tailwind important) beats the base
        DialogContent's sm:max-w-lg. Height capped at 90vh so a long
        snippet/notes block scrolls inside the modal instead of
        pushing it past the viewport. Body is the single scroll
        region via flex-1 min-h-0 overflow-y-auto.
      */}
      <DialogContent className="sm:!max-w-3xl w-[calc(100vw-2rem)] bg-card border-border text-card-foreground p-0 gap-0 max-h-[90vh] flex flex-col">
        <DialogHeader className="px-6 pt-6 pb-4 border-b border-border flex-shrink-0">
          <DialogTitle className="flex items-start justify-between pr-8 gap-3">
            {/* Title wraps up to 3 lines via line-clamp-3 break-words —
                NOT truncate, which silently drops chars from long
                agent_ids. */}
            <span className="text-lg font-semibold break-words line-clamp-3 leading-snug">
              Agent {agent.agent_id}
            </span>
            <div className="flex items-center gap-2 flex-shrink-0 pt-0.5">
              <Badge variant="outline" className="text-xs">
                {agent.status}
              </Badge>
            </div>
          </DialogTitle>
          <DialogDescription>
            All fields for{' '}
            <code className="font-mono [overflow-wrap:anywhere]">
              {agent.agent_id}
            </code>
            , plus copy-paste-ready MCP client config.
          </DialogDescription>
        </DialogHeader>

        {/*
          Scrollable body. flex-1 min-h-0 overflow-y-auto means this
          region expands to fill the remaining DialogContent height
          and is the single thing that scrolls — header + footer are
          flex-shrink-0 and stay pinned.
        */}
        <div className="px-6 py-4 flex-1 min-h-0 overflow-y-auto space-y-4 text-sm">
          {/* Group 1: identity + status */}
          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Agent ID
              </Label>
              <div className="font-mono text-sm [overflow-wrap:anywhere] flex items-center gap-2">
                <span>{agent.agent_id}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={copyAgentId}
                  className="h-6 w-6 p-0 flex-shrink-0"
                  title="Copy agent id"
                >
                  <Copy className="h-3 w-3" />
                </Button>
                {copied && <span className="text-xs text-primary">copied</span>}
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Status
              </Label>
              <div>
                <Badge variant="outline">{agent.status}</Badge>
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Created
              </Label>
              <div className="text-sm [overflow-wrap:anywhere]">
                {agent.created_at && agent.created_at !== 'N/A'
                  ? `${new Date(agent.created_at).toLocaleString()} (${formatRelative(agent.created_at)})`
                  : 'N/A'}
              </div>
            </div>
          </div>

          {agent.terminated_at && (
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Terminated
              </Label>
              <div className="text-sm [overflow-wrap:anywhere]">
                {new Date(agent.terminated_at).toLocaleString()} (
                {formatRelative(agent.terminated_at)})
              </div>
            </div>
          )}

          {/* Group 2: capabilities / wd / color */}
          <div className="border-t border-border pt-4 space-y-3">
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Capabilities
              </Label>
              <div className="text-sm [overflow-wrap:anywhere]">
                {(() => {
                  const caps = normalizeCapabilities(agent.capabilities)
                  return caps.length > 0
                    ? caps.join(', ')
                    : <span className="text-muted-foreground italic">none</span>
                })()}
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Working Directory
              </Label>
              <div className="font-mono text-xs [overflow-wrap:anywhere]">
                {agent.working_directory || (
                  <span className="text-muted-foreground italic font-sans">unset</span>
                )}
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Color
              </Label>
              <div className="flex items-center gap-2">
                {agent.color ? (
                  <>
                    <span
                      className="inline-block w-4 h-4 rounded border border-border"
                      style={{ backgroundColor: agent.color }}
                    />
                    <code className="font-mono text-xs">{agent.color}</code>
                  </>
                ) : (
                  <span className="text-muted-foreground italic">unset</span>
                )}
              </div>
            </div>
          </div>

          {/* Group 3: current task */}
          <div className="border-t border-border pt-4 space-y-1">
            <Label className="text-xs text-muted-foreground uppercase tracking-wider">
              Current Task
            </Label>
            <div className="text-sm">
              {currentTask ? (
                <button
                  className="text-primary hover:underline text-left [overflow-wrap:anywhere]"
                  onClick={() => onTaskClick(currentTask)}
                >
                  {currentTask.title}{' '}
                  <span className="text-xs text-muted-foreground font-mono">
                    ({currentTask.task_id.slice(-8)})
                  </span>
                </button>
              ) : (
                <span className="text-muted-foreground italic">none</span>
              )}
            </div>
          </div>

          {/* Group 4: token (32-hex blob; uses [overflow-wrap:anywhere]) */}
          <div className="border-t border-border pt-4 space-y-1">
            <Label className="text-xs text-muted-foreground uppercase tracking-wider">
              Token
            </Label>
            <div className="flex items-start gap-2 flex-wrap">
              {agent.auth_token ? (
                <>
                  <code className="font-mono text-xs [overflow-wrap:anywhere] flex-1 min-w-0">
                    {revealToken
                      ? agent.auth_token
                      : `...${agent.auth_token.slice(-4)}`}
                  </code>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setRevealToken((v) => !v)}
                    className="h-6 px-2 text-xs flex-shrink-0"
                  >
                    {revealToken ? 'Hide' : 'Reveal'}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      navigator.clipboard.writeText(agent.auth_token || '')
                    }}
                    className="h-6 w-6 p-0 flex-shrink-0"
                    title="Copy token"
                  >
                    <Copy className="h-3 w-3" />
                  </Button>
                </>
              ) : (
                <span className="text-muted-foreground italic">none</span>
              )}
            </div>
          </div>

          {/* Group 5: MCP-onboarding tabs ------------------------------
              One tab per supported client. Each tab body is a
              copy-paste-ready snippet wired to this agent's id +
              token + the path-prefix-derived URL. Active tab persists
              to localStorage under ACTIVE_TAB_STORAGE_KEY so a user's
              "I always use OpenCode" preference survives reloads. */}
          <div className="border-t border-border pt-4 space-y-2">
            <Label className="text-xs text-muted-foreground uppercase tracking-wider">
              Add as MCP server
            </Label>
            <p className="text-xs text-muted-foreground">
              Streamable HTTP transport (MCP spec rev 2025-03-26). Server name is
              namespaced per agent_id so multi-agent setups don&apos;t collide.
            </p>
            {/*
              Tabs are expanded statically (one TabsTrigger / TabsContent
              per client) rather than .map()'d so the literal client
              values are greppable / regression-guard-friendly. The
              buildSnippet helper still owns all the per-client config
              schema knowledge; this block just wires it up.

              Snippet format details:
              - Server name: `agent-mcp-${agent.agent_id}` so each
                agent registers under a unique key in multi-agent
                identity setups.
              - URL: derived from window.location.origin +
                projectContext.apiPrefix + '/mcp' (path-prefix adapter,
                PR #56). The /mcp endpoint is Streamable HTTP per
                MCP spec rev 2025-03-26 (PR #61).
              - Authorization: Bearer <agent_token> on every snippet.
            */}
            <Tabs value={activeTab} onValueChange={handleTabChange} className="w-full">
              <TabsList className="flex flex-wrap h-auto justify-start gap-1">
                <TabsTrigger value="claude-code" className="text-xs">Claude Code</TabsTrigger>
                <TabsTrigger value="opencode" className="text-xs">OpenCode</TabsTrigger>
                <TabsTrigger value="cursor" className="text-xs">Cursor</TabsTrigger>
                <TabsTrigger value="cline" className="text-xs">Cline</TabsTrigger>
                <TabsTrigger value="zed" className="text-xs">Zed</TabsTrigger>
                <TabsTrigger value="continue" className="text-xs">Continue.dev</TabsTrigger>
                <TabsTrigger value="generic" className="text-xs">Generic JSON</TabsTrigger>
              </TabsList>
              <TabsContent value="claude-code" className="mt-2">
                <SnippetBlock
                  snippet={buildSnippet('claude-code', agent.agent_id, snippetToken, mcpUrl)}
                  copied={copiedSnippet === 'claude-code'}
                  onCopy={() => handleCopySnippet('claude-code')}
                />
              </TabsContent>
              <TabsContent value="opencode" className="mt-2">
                <SnippetBlock
                  snippet={buildSnippet('opencode', agent.agent_id, snippetToken, mcpUrl)}
                  copied={copiedSnippet === 'opencode'}
                  onCopy={() => handleCopySnippet('opencode')}
                />
              </TabsContent>
              <TabsContent value="cursor" className="mt-2">
                <SnippetBlock
                  snippet={buildSnippet('cursor', agent.agent_id, snippetToken, mcpUrl)}
                  copied={copiedSnippet === 'cursor'}
                  onCopy={() => handleCopySnippet('cursor')}
                />
              </TabsContent>
              <TabsContent value="cline" className="mt-2">
                <SnippetBlock
                  snippet={buildSnippet('cline', agent.agent_id, snippetToken, mcpUrl)}
                  copied={copiedSnippet === 'cline'}
                  onCopy={() => handleCopySnippet('cline')}
                />
              </TabsContent>
              <TabsContent value="zed" className="mt-2">
                <SnippetBlock
                  snippet={buildSnippet('zed', agent.agent_id, snippetToken, mcpUrl)}
                  copied={copiedSnippet === 'zed'}
                  onCopy={() => handleCopySnippet('zed')}
                />
              </TabsContent>
              <TabsContent value="continue" className="mt-2">
                <SnippetBlock
                  snippet={buildSnippet('continue', agent.agent_id, snippetToken, mcpUrl)}
                  copied={copiedSnippet === 'continue'}
                  onCopy={() => handleCopySnippet('continue')}
                />
              </TabsContent>
              <TabsContent value="generic" className="mt-2">
                <SnippetBlock
                  snippet={buildSnippet('generic', agent.agent_id, snippetToken, mcpUrl)}
                  copied={copiedSnippet === 'generic'}
                  onCopy={() => handleCopySnippet('generic')}
                />
              </TabsContent>
            </Tabs>
          </div>
        </div>

        <DialogFooter className="px-6 py-4 border-t border-border flex-shrink-0">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
          >
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function AgentsDashboard() {
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  const { data, loading, error, fetchAllData, refreshData, getActiveAgents } = useDataStore()
  const [searchTerm, setSearchTerm] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  // selectedAgent is the "current selection" marker the header chip
  // and the detail dialog both observe; the detail dialog's
  // open/close drives it via useDialog (see handleSelectAgent / the
  // dialog's onOpenChange below).
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null)
  // Five row-action dialogs use the live-lookup useDialog<T> hook
  // (Candidate D, architecture review 2026-06-02). Each dialog stores
  // a key (agent_id / task_id) and asks the matching selector for the
  // current row on every render — background refresh, edits, and
  // terminations all flow into the open dialog automatically.
  //
  // For purge/terminate the "row" is just the agent_id string itself
  // (those dialogs only need the id, not the row); the selector is
  // therefore an identity function that surfaces the stored key as
  // data so the dialog body stays uniform with the others.
  const agentSelector = useCallback(
    (id: string | null) =>
      id ? data?.agents?.find((a) => a.agent_id === id) ?? null : null,
    [data?.agents],
  )
  const taskByIdSelector = useCallback(
    (id: string | null) =>
      id ? data?.tasks?.find((t) => t.task_id === id) ?? null : null,
    [data?.tasks],
  )
  const identitySelector = useCallback(
    (id: string | null) => id,
    [],
  )
  const taskDialog = useDialog<Task>(taskByIdSelector)
  const purgeDialog = useDialog<string>(identitySelector)       // holds purge-target agent_id
  const terminateDialog = useDialog<string>(identitySelector)   // holds terminate-target agent_id
  const editDialog = useDialog<Agent>(agentSelector)
  const detailDialog = useDialog<Agent>(agentSelector)

  // Deleted-while-open: if the agent or task vanishes from the source
  // (terminate from another tab, etc.), the live selector returns
  // null. Auto-close so the user isn't staring at an empty modal.
  // purge/terminate dialogs are skipped — their "row" is the id itself
  // and is never null while open.
  useEffect(() => {
    if (taskDialog.isOpen && taskDialog.data === null) taskDialog.close()
  }, [taskDialog.isOpen, taskDialog.data, taskDialog.close])
  useEffect(() => {
    if (editDialog.isOpen && editDialog.data === null) editDialog.close()
  }, [editDialog.isOpen, editDialog.data, editDialog.close])
  useEffect(() => {
    if (detailDialog.isOpen && detailDialog.data === null) detailDialog.close()
  }, [detailDialog.isOpen, detailDialog.data, detailDialog.close])

  // Source list: include all agents (terminated rows need to surface so
  // admins can hit Restore/Purge on them). getActiveAgents() is kept
  // as the fallback for the "no terminated agents in this project"
  // case but extended with any terminated rows from data.agents.
  const allAgents = data?.agents || []
  const activeAgents = getActiveAgents()
  const terminatedAgents = allAgents.filter(
    (a) => a.status === 'terminated'
  )
  // Concat + dedupe by agent_id (active first, then terminated).
  const seen = new Set<string>()
  const agents = [...activeAgents, ...terminatedAgents].filter((a) => {
    if (seen.has(a.agent_id)) return false
    seen.add(a.agent_id)
    return true
  })
  const isConnected = !!activeServerId && activeServer?.status === 'connected'

  // Fetch data on mount and when server changes
  useEffect(() => {
    if (activeServerId && activeServer?.status === 'connected') {
      fetchAllData()
    }
  }, [activeServerId, activeServer?.status, fetchAllData])

  // Auto-cleanup loop removed (regression: silently terminated valid
  // worker agents every 2 minutes while the tab was open). Agent
  // termination is now strictly explicit user action via the Terminate
  // button. See tests/test_dashboard_no_auto_cleanup.py.

  const handleTaskClick = (task: Task) => {
    taskDialog.open(task.task_id)
  }

  const filteredAgents = agents.filter(agent => {
    const matchesSearch = agent.agent_id.toLowerCase().includes(searchTerm.toLowerCase()) ||
                         (agent.current_task && agent.current_task.toLowerCase().includes(searchTerm.toLowerCase()))
    const matchesStatus = statusFilter === 'all' || agent.status === statusFilter
    return matchesSearch && matchesStatus
  })

  const stats = {
    total: agents.length,
    running: agents.filter(a => a.status === 'running').length,
    pending: agents.filter(a => a.status === 'pending').length,
    failed: agents.filter(a => a.status === 'failed').length,
    totalInSystem: allAgents.length,
  }

  const handleCreateAgent = async (data: CreateAgentData) => {
    try {
      await apiClient.createAgent(data)
    } catch (error) {
      console.error('Failed to create agent:', error)
    }
  }

  const handleTerminateAgent = async (agentId: string) => {
    try {
      await apiClient.terminateAgent(agentId)
      await refreshData()
    } catch (error) {
      console.error('Failed to terminate agent:', error)
    }
  }

  const handleRestoreAgent = async (agentId: string) => {
    try {
      await apiClient.restoreAgent(agentId)
      await refreshData()
    } catch (error) {
      console.error('Failed to restore agent:', error)
    }
  }

  const handlePurgeAgent = (agentId: string) => {
    purgeDialog.open(agentId)
  }

  // Row-click handler. Opens a confirmation dialog before firing the
  // actual terminate; the in-effect idle-cleanup path uses
  // handleTerminateAgent directly so it stays bypass-confirm.
  const handleTerminateConfirm = (agentId: string) => {
    terminateDialog.open(agentId)
  }

  const handleEditAgent = (agent: Agent) => {
    editDialog.open(agent.agent_id)
  }

  const handleSelectAgent = (agent: Agent) => {
    setSelectedAgent(agent)
    detailDialog.open(agent.agent_id)
  }

  if (!isConnected) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-4">
          <Network className="h-12 w-12 text-muted-foreground mx-auto" />
          <div>
            <h3 className="text-lg font-medium text-foreground mb-2">No Server Connection</h3>
            <p className="text-muted-foreground text-sm">Connect to an MCP server to manage agents</p>
          </div>
        </div>
      </div>
    )
  }

  if (loading && agents.length === 0) {
    // CC-3 audit 2026-06-02: replaced the spinner+"Loading agents..."
    // placeholder with a Skeleton shape that mirrors the stats + table
    // layout. Reads as the page populating in place rather than the
    // dashboard being broken.
    return (
      <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6">
        <div className="grid gap-3 sm:gap-4 grid-cols-2 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <Skeleton className="h-10 w-full sm:max-w-md" />
        <div className="bg-card border border-border rounded-lg p-4 space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-14 w-full" />
          ))}
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
    <React.Profiler id="AgentsDashboard" onRender={onRender}>
      <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          {/* CC-8 audit 2026-06-02: plain Tailwind h1 sizing. */}
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-foreground">Agent Fleet</h1>
          <p className="text-muted-foreground text-sm sm:text-base mt-1">Monitor and manage autonomous agents</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          {/* CC-19 audit 2026-06-02: dropped animate-pulse on the
              server-online dot. */}
          <Badge variant="outline" className="text-xs bg-primary/15 text-primary border-primary/30 font-medium">
            <span aria-hidden className="w-2 h-2 bg-primary rounded-full mr-2" />
            {activeServer?.name}
          </Badge>
          {data?.timestamp && (
            <span className="text-xs text-muted-foreground">
              Last updated: {new Date(data.timestamp).toLocaleTimeString()}
            </span>
          )}
          <Button
            variant="outline" 
            size="sm" 
            onClick={refreshData}
            disabled={loading}
            className="text-xs"
          >
            <RefreshCw className={cn("h-3.5 w-3.5 mr-1.5", loading && "animate-spin")} />
            Refresh
          </Button>
          <CreateAgentModal onCreateAgent={handleCreateAgent} />
        </div>
      </div>

      {/* Stats */}
      <div className="grid gap-3 sm:gap-4 grid-cols-2 sm:grid-cols-4">
        <StatsCard 
          icon={Users} 
          label="Total" 
          value={stats.total} 
          change={stats.total > 0 ? `${stats.running} active` : undefined}
          trend="neutral"
        />
        <StatsCard 
          icon={CheckCircle2} 
          label="Running" 
          value={stats.running} 
          change={stats.total > 0 ? `${Math.round((stats.running/stats.total)*100)}%` : "0%"}
          trend="up"
        />
        <StatsCard 
          icon={Clock} 
          label="Pending" 
          value={stats.pending} 
          change={stats.pending > 0 ? "Waiting" : "None"}
          trend="neutral"
        />
        <StatsCard 
          icon={AlertCircle} 
          label="Failed" 
          value={stats.failed} 
          change={stats.failed > 0 ? "Need attention" : "All good"}
          trend={stats.failed > 0 ? "down" : "neutral"}
        />
      </div>

      {/* Controls */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 sm:gap-3">
        <div className="relative flex-1 sm:max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search agents..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="pl-10 bg-background border-border text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:ring-primary/20 transition-all"
          />
        </div>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-full sm:w-32 bg-background border-border text-foreground">
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="bg-background border-border">
            <SelectItem value="all">All Status</SelectItem>
            <SelectItem value="running">Running</SelectItem>
            <SelectItem value="pending">Pending</SelectItem>
            <SelectItem value="terminated">Terminated</SelectItem>
            <SelectItem value="failed">Failed</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Agents list — CC-4/CC-6/CC-7 audit 2026-06-02: dropped
          bg-card/30 + backdrop-blur (modern-minimal calls for no
          ambient depth), desktop renders <Table>, mobile renders
          <AgentsMobileList> (card-list), empty state uses shared
          <EmptyState>. */}
      <div className="bg-card border border-border rounded-lg overflow-hidden">
        {filteredAgents.length === 0 ? (
          <EmptyState
            icon={Users}
            title="No agents found"
            description={
              agents.length === 0
                ? "Deploy your first agent to get started."
                : "No agents match your current filters."
            }
            action={
              agents.length === 0
                ? <CreateAgentModal onCreateAgent={handleCreateAgent} />
                : undefined
            }
          />
        ) : (
          <>
            {/* Desktop table */}
            <div className="hidden sm:block overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="border-border hover:bg-transparent">
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider">Agent</TableHead>
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider">Status</TableHead>
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider">Tasks</TableHead>
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider">Token</TableHead>
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider w-24">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredAgents.map((agent) => (
                    <CompactAgentRow
                      key={agent.agent_id}
                      agent={agent}
                      onTerminate={handleTerminateConfirm}
                      onRestore={handleRestoreAgent}
                      onPurge={handlePurgeAgent}
                      openView={handleSelectAgent}
                      onEdit={handleEditAgent}
                      onTaskClick={handleTaskClick}
                    />
                  ))}
                </TableBody>
              </Table>
            </div>
            {/* Mobile card-list (CC-7) */}
            <div className="block sm:hidden">
              <AgentsMobileList
                agents={filteredAgents}
                openView={handleSelectAgent}
                onEdit={handleEditAgent}
                onTerminate={handleTerminateConfirm}
                onRestore={handleRestoreAgent}
                onPurge={handlePurgeAgent}
              />
            </div>
          </>
        )}
      </div>

      {/* Agent Detail Modal — replaces the old sidebar drawer */}
      <AgentDetailDialog
        agent={detailDialog.data}
        open={detailDialog.isOpen}
        onOpenChange={(open) => {
          if (!open) {
            detailDialog.close()
            setSelectedAgent(null)
          }
        }}
        onTaskClick={(task) => {
          handleTaskClick(task)
        }}
      />

      {/* Edit Agent Dialog */}
      <EditAgentDialog
        agent={editDialog.data}
        open={editDialog.isOpen}
        onOpenChange={(open) => {
          if (!open) editDialog.close()
        }}
        onSaved={() => {
          void refreshData()
        }}
      />

      {/* Terminate confirmation dialog */}
      <TerminateAgentDialog
        agentId={terminateDialog.data}
        open={terminateDialog.isOpen}
        onOpenChange={(open) => {
          if (!open) terminateDialog.close()
        }}
        onConfirmed={async (agentId) => {
          await handleTerminateAgent(agentId)
        }}
      />

      {/* Task Details Dialog */}
      <TaskDetailsDialog
        task={taskDialog.data}
        open={taskDialog.isOpen}
        onOpenChange={(open) => {
          if (!open) taskDialog.close()
        }}
      />

      {/* Purge confirmation dialog (cascade tombstone + DELETE) */}
      <PurgeAgentDialog
        agentId={purgeDialog.data}
        open={purgeDialog.isOpen}
        onOpenChange={(open) => {
          if (!open) purgeDialog.close()
        }}
        onConfirmed={() => {
          void refreshData()
        }}
      />
      </div>
    </React.Profiler>
  )
}