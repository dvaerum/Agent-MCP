"use client"

// Schedules dashboard (event-loop scheduler, plan §5.5). The operator's
// visual control surface for every scheduled directive in the project:
// an all-schedules table with an inline enable/disable toggle, create /
// edit / delete modals, and a per-agent "poke" (ad-hoc directive) button.
// Backed by the operator-gated /api/schedules REST routes + the
// /api/agents/{id}/directive poke route.

import React, { useCallback, useEffect, useMemo, useState } from "react"
import { CalendarClock, Pencil, Trash2, Send, RefreshCw, Plus } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from "@/components/ui/tooltip"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/dashboard/shared/empty-state"
import { SendDirectiveModal } from "@/components/dashboard/shared/send-directive-modal"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { apiClient, type Agent, type Schedule } from "@/lib/api"
import {
  agentsInSchedules, filterSchedules, formatAbsolute, formatEndCondition,
  formatInterval, formatNextFire, sortByNextFire, type StatusFilter,
} from "@/lib/schedules"

const STATUS_BADGE: Record<string, string> = {
  active: "border-green-500/40 text-green-600 dark:text-green-400",
  paused: "border-amber-500/40 text-amber-600 dark:text-amber-400",
  completed: "border-muted-foreground/30 text-muted-foreground",
}

interface FormState {
  agent_id: string
  prompt: string
  interval_seconds: string
  until: string
  count: string
  run_now: boolean
}

const EMPTY_FORM: FormState = {
  agent_id: "",
  prompt: "",
  interval_seconds: "60",
  until: "",
  count: "",
  run_now: false,
}

function toIsoOrNull(local: string): string | null {
  if (!local) return null
  const d = new Date(local)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}

export function SchedulesDashboard() {
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)
  const [floor, setFloor] = useState<number>(60)
  const [maxPerAgent, setMaxPerAgent] = useState<number>(10)

  const [agentFilter, setAgentFilter] = useState<string>("all")
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all")

  // create/edit modal
  const [formOpen, setFormOpen] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)

  // delete confirm
  const [deleteId, setDeleteId] = useState<string | null>(null)

  // Send-directive (poke) modal — shared with the Agents page.
  // `directiveOpen` drives visibility; `directiveAgent` is the locked
  // target for the per-row shortcut, or null for the standalone
  // top-of-page control (which renders an agent picker).
  const [directiveOpen, setDirectiveOpen] = useState(false)
  const [directiveAgent, setDirectiveAgent] = useState<string | null>(null)

  const openDirective = (agentId: string | null) => {
    setDirectiveAgent(agentId)
    setDirectiveOpen(true)
  }

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [rows, agentRows] = await Promise.all([
        apiClient.getSchedules(),
        apiClient.getAgents().catch(() => [] as Agent[]),
      ])
      setSchedules(rows)
      setAgents(agentRows)
    } catch (e) {
      toastError(e, "Failed to load schedules")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  // Guardrail floor/max shown inline in the create/edit form.
  useEffect(() => {
    void (async () => {
      try {
        const { schema } = await apiClient.getSettingsSchema()
        for (const s of schema) {
          if (s.key === "config_min_schedule_interval_seconds") {
            setFloor(Number(s.default) || 60)
          } else if (s.key === "config_max_schedules_per_agent") {
            setMaxPerAgent(Number(s.default) || 10)
          }
        }
      } catch { /* defaults stand */ }
    })()
  }, [])

  const agentOptions = useMemo(() => {
    const fromAgents = agents.map((a) => a.agent_id)
    const merged = new Set<string>([...fromAgents, ...agentsInSchedules(schedules)])
    return Array.from(merged).filter(Boolean).sort()
  }, [agents, schedules])

  const visible = useMemo(
    () => sortByNextFire(filterSchedules(schedules, agentFilter, statusFilter)),
    [schedules, agentFilter, statusFilter],
  )

  const openCreate = () => {
    setEditId(null)
    setForm({ ...EMPTY_FORM, agent_id: agentOptions[0] ?? "" })
    setFormOpen(true)
  }

  const openEdit = (s: Schedule) => {
    setEditId(s.directive_id)
    setForm({
      agent_id: s.agent_id,
      prompt: s.prompt,
      interval_seconds: String(s.interval_seconds),
      until: "",
      count: s.max_runs != null ? String(s.max_runs) : "",
      run_now: false,
    })
    setFormOpen(true)
  }

  const submitForm = async () => {
    setSaving(true)
    try {
      const interval = Number(form.interval_seconds)
      if (editId) {
        await apiClient.updateSchedule(editId, {
          prompt: form.prompt,
          interval_seconds: interval,
          until: form.until ? toIsoOrNull(form.until) : undefined,
          count: form.count ? Number(form.count) : undefined,
        })
        toastSuccess("Schedule updated")
      } else {
        await apiClient.createSchedule({
          agent_id: form.agent_id,
          prompt: form.prompt,
          interval_seconds: interval,
          until: toIsoOrNull(form.until),
          count: form.count ? Number(form.count) : null,
          run_now: form.run_now,
        })
        toastSuccess("Schedule created")
      }
      setFormOpen(false)
      await load()
    } catch (e) {
      toastError(e, "Failed to save schedule")
    } finally {
      setSaving(false)
    }
  }

  const toggleEnabled = async (s: Schedule, next: boolean) => {
    // Optimistic flip; revert on error.
    setSchedules((prev) => prev.map((x) =>
      x.directive_id === s.directive_id ? { ...x, enabled: next } : x))
    try {
      await apiClient.updateSchedule(s.directive_id, { enabled: next })
      await load()
    } catch (e) {
      toastError(e, "Failed to update schedule")
      setSchedules((prev) => prev.map((x) =>
        x.directive_id === s.directive_id ? { ...x, enabled: s.enabled } : x))
    }
  }

  const confirmDelete = async () => {
    if (!deleteId) return
    try {
      await apiClient.deleteSchedule(deleteId)
      toastSuccess("Schedule deleted")
      setDeleteId(null)
      await load()
    } catch (e) {
      toastError(e, "Failed to delete schedule")
    }
  }

  return (
    <div className="space-y-4 p-1" data-testid="schedules-dashboard">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <CalendarClock className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-xl font-semibold">Schedules</h1>
          <Badge variant="outline">{schedules.length}</Badge>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void load()}
                  aria-label="Refresh schedules">
            <RefreshCw className="h-4 w-4" />
          </Button>
          {/* Standalone send-directive control — NOT tied to a schedule
              row, so any agent (schedule or not) can be poked from here.
              Opens the shared modal with an agent picker. */}
          <Button variant="outline" size="sm" onClick={() => openDirective(null)}
                  data-testid="send-directive-btn">
            <Send className="mr-1 h-4 w-4" /> Send directive
          </Button>
          <Button size="sm" onClick={openCreate} data-testid="new-schedule-btn">
            <Plus className="mr-1 h-4 w-4" /> New schedule
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Select value={agentFilter} onValueChange={setAgentFilter}>
          <SelectTrigger className="w-[180px]" aria-label="Filter by agent">
            <SelectValue placeholder="All agents" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All agents</SelectItem>
            {agentOptions.map((a) => (
              <SelectItem key={a} value={a}>{a}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={statusFilter}
                onValueChange={(v) => setStatusFilter(v as StatusFilter)}>
          <SelectTrigger className="w-[160px]" aria-label="Filter by status">
            <SelectValue placeholder="All statuses" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="active">Active</SelectItem>
            <SelectItem value="paused">Paused</SelectItem>
            <SelectItem value="completed">Completed</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm text-muted-foreground">
            All scheduled directives
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="space-y-2">
              {[0, 1, 2].map((i) => <Skeleton key={i} className="h-10 w-full" />)}
            </div>
          ) : visible.length === 0 ? (
            <EmptyState
              icon={CalendarClock}
              title="No schedules"
              description="Create a recurring directive for an agent to get started."
            />
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Enabled</TableHead>
                    <TableHead>Agent</TableHead>
                    <TableHead>Directive</TableHead>
                    <TableHead>Interval</TableHead>
                    <TableHead>Next fire</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Runs</TableHead>
                    <TableHead>End</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {visible.map((s) => (
                    <TableRow key={s.directive_id}
                              data-testid={`schedule-row-${s.directive_id}`}>
                      <TableCell>
                        <Switch
                          checked={s.enabled}
                          disabled={s.status === "completed"}
                          onCheckedChange={(v) => void toggleEnabled(s, v)}
                          aria-label={`Toggle schedule ${s.directive_id}`}
                          data-testid={`toggle-${s.directive_id}`}
                        />
                      </TableCell>
                      <TableCell className="font-medium">{s.agent_id}</TableCell>
                      <TableCell className="max-w-[260px]">
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="block truncate">{s.prompt}</span>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-sm">
                              {s.prompt}
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      </TableCell>
                      <TableCell>{formatInterval(s.interval_seconds)}</TableCell>
                      <TableCell>
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span>{formatNextFire(s.next_due_at)}</span>
                            </TooltipTrigger>
                            <TooltipContent>
                              {formatAbsolute(s.next_due_at)}
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline"
                               className={STATUS_BADGE[s.status] ?? ""}>
                          {s.status}
                        </Badge>
                      </TableCell>
                      <TableCell>{s.run_count}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {formatEndCondition(s)}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-1">
                          <Button variant="ghost" size="sm"
                                  onClick={() => openDirective(s.agent_id)}
                                  aria-label={`Poke ${s.agent_id}`}
                                  data-testid={`poke-${s.directive_id}`}>
                            <Send className="h-4 w-4" />
                          </Button>
                          <Button variant="ghost" size="sm"
                                  onClick={() => openEdit(s)}
                                  aria-label={`Edit ${s.directive_id}`}
                                  data-testid={`edit-${s.directive_id}`}>
                            <Pencil className="h-4 w-4" />
                          </Button>
                          <Button variant="ghost" size="sm"
                                  onClick={() => setDeleteId(s.directive_id)}
                                  aria-label={`Delete ${s.directive_id}`}
                                  data-testid={`delete-${s.directive_id}`}>
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Create / edit modal */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editId ? "Edit schedule" : "New schedule"}</DialogTitle>
            <DialogDescription>
              Interval floor is {floor}s; max {maxPerAgent} active schedules
              per agent.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            {!editId && (
              <div className="space-y-1">
                <Label htmlFor="sched-agent">Agent</Label>
                <Select value={form.agent_id}
                        onValueChange={(v) => setForm((f) => ({ ...f, agent_id: v }))}>
                  <SelectTrigger id="sched-agent" aria-label="Agent">
                    <SelectValue placeholder="Select an agent" />
                  </SelectTrigger>
                  <SelectContent>
                    {agentOptions.map((a) => (
                      <SelectItem key={a} value={a}>{a}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className="space-y-1">
              <Label htmlFor="sched-prompt">Directive</Label>
              <Textarea id="sched-prompt" value={form.prompt}
                        onChange={(e) => setForm((f) => ({ ...f, prompt: e.target.value }))}
                        placeholder="e.g. check the CI status and report" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label htmlFor="sched-interval">Interval (seconds)</Label>
                <Input id="sched-interval" type="number" min={floor}
                       value={form.interval_seconds}
                       onChange={(e) => setForm((f) => ({ ...f, interval_seconds: e.target.value }))} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="sched-count">Max runs (optional)</Label>
                <Input id="sched-count" type="number" min={1}
                       value={form.count}
                       onChange={(e) => setForm((f) => ({ ...f, count: e.target.value }))} />
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="sched-until">Until (optional)</Label>
              <Input id="sched-until" type="datetime-local"
                     value={form.until}
                     onChange={(e) => setForm((f) => ({ ...f, until: e.target.value }))} />
            </div>
            {!editId && (
              <label className="flex items-center gap-2 text-sm">
                <Switch checked={form.run_now}
                        onCheckedChange={(v) => setForm((f) => ({ ...f, run_now: v }))}
                        aria-label="Run now" />
                Fire once immediately (run now)
              </label>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setFormOpen(false)}>
              Cancel
            </Button>
            <Button onClick={() => void submitForm()}
                    disabled={saving || !form.prompt || (!editId && !form.agent_id)}
                    data-testid="save-schedule-btn">
              {saving ? "Saving…" : editId ? "Save" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <Dialog open={deleteId != null} onOpenChange={(o) => !o && setDeleteId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete schedule</DialogTitle>
            <DialogDescription>
              This permanently removes the scheduled directive. This cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteId(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void confirmDelete()}
                    data-testid="confirm-delete-btn">
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Send-directive (poke) modal — shared with the Agents page.
          `directiveAgent` is a locked target (per-row shortcut) or null
          for the standalone picker. */}
      <SendDirectiveModal
        open={directiveOpen}
        onOpenChange={setDirectiveOpen}
        lockedAgentId={directiveAgent}
      />
    </div>
  )
}
