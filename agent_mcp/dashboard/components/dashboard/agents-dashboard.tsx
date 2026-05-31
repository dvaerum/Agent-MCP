"use client"

import React, { useState, useEffect } from "react"
import {
  Users, Clock, AlertCircle, CheckCircle2, Shield, Cpu, Database, Network, Terminal,
  Search, Plus, Eye, RefreshCw, Copy, RotateCcw, Trash2, Pencil
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { apiClient, Agent, Task } from "@/lib/api"
import { useServerStore } from "@/lib/stores/server-store"
import { useDataStore } from "@/lib/stores/data-store"
import { cn } from "@/lib/utils"
import { TaskDetailsDialog } from "./task-details-dialog"


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

const CompactAgentRow = React.memo(({ agent, onTerminate, onRestore, onPurge, onSelect, onEdit, onTaskClick }: {
  agent: Agent,
  onTerminate: (id: string) => void,
  onRestore: (id: string) => void,
  onPurge: (id: string) => void,
  onSelect: (agent: Agent) => void,
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
    <TableRow className="border-border/50 hover:bg-muted/30 group transition-all duration-200">
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
        </div>
      </TableCell>
      
      <TableCell className="py-3 max-w-xs">
        {currentTask ? (
          <div>
            <button
              onClick={() => onTaskClick(currentTask)}
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
              onClick={() => {
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
      
      <TableCell className="py-3">
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onSelect(agent)}
            title="View details"
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
          >
            <Eye className="h-3.5 w-3.5" />
          </Button>
          {agent.agent_id !== 'Admin' && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onEdit(agent)}
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
              onClick={() => onTerminate(agent.agent_id)}
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
                onClick={() => onRestore(agent.agent_id)}
                title="Restore"
                className="h-7 px-2 text-xs text-primary hover:text-primary/80 hover:bg-primary/10"
              >
                <RotateCcw className="h-3.5 w-3.5 mr-1" />
                Restore
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onPurge(agent.agent_id)}
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
  <div className="bg-card/80 border border-border/60 rounded-xl p-[var(--space-fluid-md)] backdrop-blur-sm hover:bg-card transition-all duration-200 group">
    <div className="flex items-center justify-between">
      <div>
        <div className="flex items-center gap-2 mb-2">
          <Icon className="h-4 w-4 text-muted-foreground group-hover:text-foreground transition-colors" />
          <span className="text-fluid-xs font-semibold text-muted-foreground uppercase tracking-wider">{label}</span>
        </div>
        <div className="text-fluid-2xl font-bold text-foreground mb-1">{value}</div>
        {change && (
          <div className={cn(
            "text-fluid-xs font-medium",
            trend === 'up' && "text-primary",
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
      <DialogContent className="sm:max-w-md bg-card border-border text-card-foreground">
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
      <DialogContent className="sm:max-w-md bg-card border-border text-card-foreground">
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
      <DialogContent className="sm:max-w-md bg-card border-border text-card-foreground">
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
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !agent) return
    setCapabilities((agent.capabilities || []).join(', '))
    setColor(agent.color || '')
    setWorkingDirectory(agent.working_directory || '')
    setError(null)
  }, [open, agent])

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!agent) return
    setBusy(true)
    setError(null)
    const updates: { capabilities?: string[]; color?: string; working_directory?: string } = {}
    const parsedCaps = capabilities
      .split(',')
      .map((c) => c.trim())
      .filter((c) => c.length > 0)
    if (JSON.stringify(parsedCaps) !== JSON.stringify(agent.capabilities || [])) {
      updates.capabilities = parsedCaps
    }
    if (color !== (agent.color || '')) {
      updates.color = color
    }
    if (workingDirectory !== (agent.working_directory || '')) {
      updates.working_directory = workingDirectory
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
      <DialogContent className="sm:max-w-md bg-card border-border text-card-foreground">
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

// Read-only agent details modal — replaces the old sidebar drawer.
// Uses the same Dialog primitive as the Messages row-detail popup
// (PR #36).
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
  const { getAgentTasks } = useDataStore()

  useEffect(() => {
    if (!open) {
      setRevealToken(false)
      setCopied(false)
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

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl bg-card border-border text-card-foreground">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            Agent details
            <Badge variant="outline" className="text-xs">
              {agent.status}
            </Badge>
          </DialogTitle>
          <DialogDescription>
            All fields for <code>{agent.agent_id}</code>.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <div className="grid grid-cols-3 gap-2">
            <div className="text-muted-foreground">Agent ID</div>
            <div className="col-span-2 font-mono break-all flex items-center gap-2">
              <span>{agent.agent_id}</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={copyAgentId}
                className="h-6 w-6 p-0"
                title="Copy agent id"
              >
                <Copy className="h-3 w-3" />
              </Button>
              {copied && <span className="text-xs text-primary">copied</span>}
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="text-muted-foreground">Status</div>
            <div className="col-span-2">
              <Badge variant="outline">{agent.status}</Badge>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="text-muted-foreground">Created</div>
            <div className="col-span-2">
              {agent.created_at && agent.created_at !== 'N/A'
                ? `${new Date(agent.created_at).toLocaleString()} (${formatRelative(agent.created_at)})`
                : 'N/A'}
            </div>
          </div>
          {agent.terminated_at && (
            <div className="grid grid-cols-3 gap-2">
              <div className="text-muted-foreground">Terminated</div>
              <div className="col-span-2">
                {new Date(agent.terminated_at).toLocaleString()} (
                {formatRelative(agent.terminated_at)})
              </div>
            </div>
          )}
          <div className="grid grid-cols-3 gap-2">
            <div className="text-muted-foreground">Capabilities</div>
            <div className="col-span-2">
              {agent.capabilities && agent.capabilities.length > 0
                ? agent.capabilities.join(', ')
                : <span className="text-muted-foreground">none</span>}
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="text-muted-foreground">Working Directory</div>
            <div className="col-span-2 font-mono break-all">
              {agent.working_directory || <span className="text-muted-foreground">unset</span>}
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="text-muted-foreground">Color</div>
            <div className="col-span-2 flex items-center gap-2">
              {agent.color ? (
                <>
                  <span
                    className="inline-block w-4 h-4 rounded border border-border"
                    style={{ backgroundColor: agent.color }}
                  />
                  <code className="font-mono">{agent.color}</code>
                </>
              ) : (
                <span className="text-muted-foreground">unset</span>
              )}
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="text-muted-foreground">Current Task</div>
            <div className="col-span-2">
              {currentTask ? (
                <button
                  className="text-primary hover:underline text-left"
                  onClick={() => onTaskClick(currentTask)}
                >
                  {currentTask.title}{' '}
                  <span className="text-xs text-muted-foreground font-mono">
                    ({currentTask.task_id.slice(-8)})
                  </span>
                </button>
              ) : (
                <span className="text-muted-foreground">none</span>
              )}
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <div className="text-muted-foreground">Token</div>
            <div className="col-span-2 flex items-center gap-2">
              {agent.auth_token ? (
                <>
                  <code className="font-mono text-xs break-all">
                    {revealToken
                      ? agent.auth_token
                      : `...${agent.auth_token.slice(-4)}`}
                  </code>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setRevealToken((v) => !v)}
                    className="h-6 px-2 text-xs"
                  >
                    {revealToken ? 'Hide' : 'Reveal'}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      navigator.clipboard.writeText(agent.auth_token || '')
                    }}
                    className="h-6 w-6 p-0"
                    title="Copy token"
                  >
                    <Copy className="h-3 w-3" />
                  </Button>
                </>
              ) : (
                <span className="text-muted-foreground">none</span>
              )}
            </div>
          </div>
        </div>
        <DialogFooter>
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
  const { data, loading, error, fetchAllData, refreshData, getActiveAgents, getIdleAgentsForCleanup } = useDataStore()
  const [searchTerm, setSearchTerm] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null)
  const [selectedTask, setSelectedTask] = useState<Task | null>(null)
  const [taskDialogOpen, setTaskDialogOpen] = useState(false)
  const [purgeTargetId, setPurgeTargetId] = useState<string | null>(null)
  const [purgeDialogOpen, setPurgeDialogOpen] = useState(false)
  const [terminateTargetId, setTerminateTargetId] = useState<string | null>(null)
  const [terminateDialogOpen, setTerminateDialogOpen] = useState(false)
  const [editAgent, setEditAgent] = useState<Agent | null>(null)
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [detailAgent, setDetailAgent] = useState<Agent | null>(null)
  const [detailDialogOpen, setDetailDialogOpen] = useState(false)

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

  // Automatic agent cleanup - check every 2 minutes
  useEffect(() => {
    if (!isConnected) return

    const cleanupInterval = setInterval(async () => {
      const idleAgents = getIdleAgentsForCleanup()
      
      if (idleAgents.length > 0) {
        console.log(`🧹 Found ${idleAgents.length} idle agents for cleanup:`, idleAgents.map(a => a.agent_id))
        
        // Terminate each idle agent
        for (const agent of idleAgents) {
          try {
            await handleTerminateAgent(agent.agent_id)
            console.log(`✅ Terminated idle agent: ${agent.agent_id}`)
          } catch (error) {
            console.error(`❌ Failed to terminate idle agent ${agent.agent_id}:`, error)
          }
        }
        
        // Refresh data after cleanup
        await refreshData()
      }
    }, 2 * 60 * 1000) // Check every 2 minutes

    return () => clearInterval(cleanupInterval)
  }, [isConnected, getIdleAgentsForCleanup, refreshData])
  
  
  const handleTaskClick = (task: Task) => {
    setSelectedTask(task)
    setTaskDialogOpen(true)
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
    // Also track cleanup statistics
    totalInSystem: allAgents.length,
    idleForCleanup: getIdleAgentsForCleanup().length
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
    setPurgeTargetId(agentId)
    setPurgeDialogOpen(true)
  }

  // Row-click handler. Opens a confirmation dialog before firing the
  // actual terminate; the in-effect idle-cleanup path uses
  // handleTerminateAgent directly so it stays bypass-confirm.
  const handleTerminateConfirm = (agentId: string) => {
    setTerminateTargetId(agentId)
    setTerminateDialogOpen(true)
  }

  const handleEditAgent = (agent: Agent) => {
    setEditAgent(agent)
    setEditDialogOpen(true)
  }

  const handleSelectAgent = (agent: Agent) => {
    setSelectedAgent(agent)
    setDetailAgent(agent)
    setDetailDialogOpen(true)
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

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-4">
          <div className="animate-spin h-8 w-8 border-2 border-primary border-t-transparent rounded-full mx-auto" />
          <p className="text-muted-foreground text-sm">Loading agents...</p>
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
      <div className="w-full space-y-[var(--space-fluid-lg)] -mx-[var(--container-padding)] px-[var(--container-padding)] -my-[var(--space-fluid-lg)] py-[var(--space-fluid-lg)]">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-fluid-2xl font-bold text-foreground">Agent Fleet</h1>
          <p className="text-muted-foreground text-fluid-base mt-1">Monitor and manage autonomous agents</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          <Badge variant="outline" className="text-xs bg-primary/15 text-primary border-primary/30 font-medium">
            <div className="w-2 h-2 bg-primary rounded-full mr-2 animate-pulse" />
            {activeServer?.name}
          </Badge>
          {data?.timestamp && (
            <span className="text-xs text-muted-foreground">
              Last updated: {new Date(data.timestamp).toLocaleTimeString()}
            </span>
          )}
          {stats.idleForCleanup > 0 && (
            <Badge variant="outline" className="text-xs bg-orange-500/15 text-orange-600 border-orange-500/30 font-medium">
              {stats.idleForCleanup} pending cleanup
            </Badge>
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
      <div className="grid gap-[var(--space-fluid-md)] grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
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
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-[var(--space-fluid-sm)]">
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

      {/* Agents Table */}
      <div className="bg-card/30 border border-border/50 rounded-lg backdrop-blur-sm overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="border-border/50 hover:bg-transparent">
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
                onSelect={handleSelectAgent}
                onEdit={handleEditAgent}
                onTaskClick={handleTaskClick}
              />
            ))}
          </TableBody>
        </Table>
        
        {filteredAgents.length === 0 && (
          <div className="p-12 text-center">
            <Users className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
            <h3 className="text-lg font-medium text-foreground mb-2">No agents found</h3>
            <p className="text-muted-foreground text-sm mb-4">
              {agents.length === 0 ? "Deploy your first agent to get started" : "No agents match your current filters"}
            </p>
            {agents.length === 0 && <CreateAgentModal onCreateAgent={handleCreateAgent} />}
          </div>
        )}
      </div>

      {/* Agent Detail Modal — replaces the old sidebar drawer */}
      <AgentDetailDialog
        agent={detailAgent}
        open={detailDialogOpen}
        onOpenChange={(open) => {
          setDetailDialogOpen(open)
          if (!open) {
            setDetailAgent(null)
            setSelectedAgent(null)
          }
        }}
        onTaskClick={(task) => {
          handleTaskClick(task)
        }}
      />

      {/* Edit Agent Dialog */}
      <EditAgentDialog
        agent={editAgent}
        open={editDialogOpen}
        onOpenChange={(open) => {
          setEditDialogOpen(open)
          if (!open) setEditAgent(null)
        }}
        onSaved={() => {
          void refreshData()
        }}
      />

      {/* Terminate confirmation dialog */}
      <TerminateAgentDialog
        agentId={terminateTargetId}
        open={terminateDialogOpen}
        onOpenChange={(open) => {
          setTerminateDialogOpen(open)
          if (!open) setTerminateTargetId(null)
        }}
        onConfirmed={async (agentId) => {
          await handleTerminateAgent(agentId)
        }}
      />

      {/* Task Details Dialog */}
      <TaskDetailsDialog
        task={selectedTask}
        open={taskDialogOpen}
        onOpenChange={(open) => {
          setTaskDialogOpen(open)
          if (!open) setSelectedTask(null)
        }}
      />

      {/* Purge confirmation dialog (cascade tombstone + DELETE) */}
      <PurgeAgentDialog
        agentId={purgeTargetId}
        open={purgeDialogOpen}
        onOpenChange={(open) => {
          setPurgeDialogOpen(open)
          if (!open) setPurgeTargetId(null)
        }}
        onConfirmed={() => {
          void refreshData()
        }}
      />
      </div>
    </React.Profiler>
  )
}