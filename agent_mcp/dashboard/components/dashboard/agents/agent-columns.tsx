"use client"

import * as React from "react"
import { useMemo, useState } from "react"
import {
  Copy, Eye, Pause, Pencil, Play, RotateCcw, Send, Trash2,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { agentPresence, type Agent, type Task } from "@/lib/api"
import { transportStatusBadge } from "@/lib/status"
import { useDataStore } from "@/lib/stores/data-store"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"
import {
  AgentTypeIcon,
  PRESENCE_BADGE_CLASS,
  PRESENCE_LABEL,
  StatusDot,
  presenceTitle,
} from "@/components/dashboard/agents/agent-presence"

/**
 * Column spec for the Agents table.
 *
 * Replaces the pre-scaffold `<CompactAgentRow>` (≈350 lines of hand-rolled
 * `<TableRow>` markup). The cells are byte-for-byte the same; what
 * changed is ownership — `<ResponsiveDataTable>` now renders the row
 * shell (`group` class, hover, row-click, the desktop/mobile twin
 * guard), and this module contributes only the per-cell content.
 *
 * Two cells keep their own component because they need hooks a plain
 * `cell: (row) => ReactNode` callback can't hold: `AgentTasksCell`
 * reads the data store, `AgentTokenCell` owns per-row "copied" state.
 */

export interface AgentRowHandlers {
  onTerminate: (id: string) => void
  onRestore: (id: string) => void
  onPurge: (id: string) => void
  openView: (agent: Agent) => void
  onEdit: (agent: Agent) => void
  onTaskClick: (task: Task) => void
  onSendDirective: (id: string) => void
  onDisconnect: (id: string) => void
  onReconnect: (id: string) => void
}

// Check if agent is new (less than 10 minutes old).
function isNewAgent(agent: Agent): boolean {
  if (agent.agent_id === 'Admin' || agent.created_at === 'N/A') return false
  const now = new Date()
  const createdAt = new Date(agent.created_at)
  const ageInMinutes = (now.getTime() - createdAt.getTime()) / (1000 * 60)
  return ageInMinutes <= 10 && !agent.current_task
}

/**
 * Task summary cell — reads the data store for the agent's tasks.
 *
 * "Assigned" here means "still on the agent's plate" — i.e. open work.
 * Finished tasks ('completed' / 'cancelled' / 'failed') must be excluded
 * so the '{n} assigned' pill matches the user's mental model. Before
 * this filter, ios-app-dev showed 18 'assigned' on washing-brothers when
 * 16 were completed.
 */
export function AgentTasksCell({
  agent,
  onTaskClick,
}: {
  agent: Agent
  onTaskClick: (task: Task) => void
}): React.ReactElement {
  const { getAgentTasks } = useDataStore()
  const agentTasks = getAgentTasks(agent.agent_id)
  const currentTask = agentTasks.find((t) => t.task_id === agent.current_task)

  // Use the data store's logic for consistent ID matching.
  const cleanAgentId = agent.agent_id.startsWith('agent_') ? agent.agent_id.substring(6) : agent.agent_id
  const normalizedAgentId = cleanAgentId === 'Admin' ? 'admin' : cleanAgentId

  const assignedTasks = agentTasks.filter(t =>
    (t.assigned_to === normalizedAgentId ||
      t.assigned_to === cleanAgentId ||
      (normalizedAgentId === 'admin' && (t.assigned_to === 'Admin' || t.assigned_to === 'admin'))) &&
    t.status !== 'completed' &&
    t.status !== 'cancelled' &&
    t.status !== 'failed'
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

  if (currentTask) {
    return (
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
    )
  }

  return (
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
  )
}

/** Token cell — owns the per-row "copied" flash. */
export function AgentTokenCell({ agent }: { agent: Agent }): React.ReactElement {
  const [copiedToken, setCopiedToken] = useState(false)
  if (!agent.auth_token) {
    return <span className="text-xs text-muted-foreground">No token</span>
  }
  return (
    <div className="flex items-center gap-2 min-w-0">
      {/* `min-w-0` on the <code>: a flex item defaults to
          `min-width:auto`, so the monospace token refuses to shrink
          below its own min-content and the transient "copied" span
          below pushes the row past the (fixed-width) cell edge.
          With it, the token truncates further for the 1.5s flash. */}
      <code className="text-xs font-mono text-muted-foreground max-w-[120px] min-w-0 truncate">
        {agent.auth_token.slice(0, 8)}...
      </code>
      <Button
        variant="ghost"
        size="sm"
        onClick={(e) => {
          e.stopPropagation()
          navigator.clipboard.writeText(agent.auth_token || '')
          setCopiedToken(true)
          setTimeout(() => setCopiedToken(false), 1500)
        }}
        className="h-6 w-6 p-0 shrink-0"
        title="Copy token"
      >
        <Copy className="h-3 w-3" />
      </Button>
      {copiedToken && <span className="text-xs text-primary shrink-0">copied</span>}
    </div>
  )
}

export function useAgentColumns(handlers: AgentRowHandlers): Column<Agent>[] {
  const {
    onTerminate, onRestore, onPurge, openView, onEdit, onTaskClick,
    onSendDirective, onDisconnect, onReconnect,
  } = handlers

  return useMemo<Column<Agent>[]>(() => [
    {
      id: 'agent',
      header: 'Agent',
      cellClassName: 'py-3',
      cell: (agent) => (
        <div className="flex items-center gap-3">
          <StatusDot presence={agentPresence(agent)} />
          <AgentTypeIcon agentId={agent.agent_id} />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-sm text-foreground truncate" title={agent.agent_id}>{agent.agent_id}</div>
            <div className="text-xs text-muted-foreground font-mono truncate">#{agent.agent_id.slice(-6)}</div>
          </div>
        </div>
      ),
    },
    {
      id: 'status',
      header: 'Status',
      // The table is `table-fixed` (see agents-dashboard.tsx), so this
      // width is the cell's HARD box — content that can't shrink or
      // wrap paints straight over the next column. w-32 (112px content
      // box) could not hold even one badge PAIR: measured live, an
      // ONLINE + WAITING row overflowed 29px into TASKS and an
      // ONLINE + WORKING + WAITING row overflowed 121px. w-44 (160px)
      // holds the common pair on one line; three badges wrap, which is
      // what `flex-wrap` below is for.
      headClassName: 'w-44',
      cellClassName: 'py-3',
      cell: (agent) => {
        // Wave 7 PR 2 — presence drives the row badge.
        const presence = agentPresence(agent)
        // ADR-0021 — delivery transport liveness. Separate axis from presence;
        // null when no delivery transport has reported for this agent.
        const transport = transportStatusBadge(agent.transport_status)
        return (
          // `flex-wrap` + `min-w-0` are load-bearing, not cosmetic:
          // every <Badge> carries `shrink-0 whitespace-nowrap` from
          // `badgeVariants`, so a non-wrapping row inside a fixed-width
          // cell has no way to stay inside it. Up to four badges can
          // land here (presence + transport + NEW + WAITING/PAUSED);
          // they now stack onto extra lines instead of painting over
          // the TASKS column. `gap-1` (not gap-2) is what lets the
          // common presence+transport pair share one line at w-44.
          <div className="flex flex-wrap items-center gap-1 min-w-0">
            {/* Wave 7 PR 2 (coordinator transition): presence badge.
                Derived from the `online` + `last_mcp_connection` fields
                served by the backend (sourced from
                `core/session_registry.py`) — NOT from the row's `status`
                column, which used to be a spawn-lifecycle artefact
                ('created' / 'pending' / 'failed') that's no longer
                meaningful now agent-mcp doesn't own the process.
                `agentPresence()` collapses the inputs into a single
                4-state enum the UI keys off of. */}
            <Badge
              variant="outline"
              title={presenceTitle(presence, agent.last_mcp_connection)}
              className={cn(
                // `max-w-full`: a wrapping row still can't contain a
                // SINGLE badge wider than the column, so cap each one
                // at the cell and let the badge's own `overflow-hidden`
                // clip it. The `title` keeps the full value reachable.
                "text-xs font-semibold border-0 px-3 py-1.5 rounded-md max-w-full",
                PRESENCE_BADGE_CLASS[presence],
              )}
            >
              {PRESENCE_LABEL[presence]}
            </Badge>
            {/* ADR-0021: delivery transport_status — a DISTINCT axis from
                the presence badge above (which is MCP-stream presence).
                Rendered only when the delivery transport has reported for
                this agent; null/absent renders nothing so the row stays
                uncluttered for agents with no delivery transport. */}
            {transport && (
              <Badge
                variant="outline"
                title={`Delivery transport: ${transport.label.toLowerCase()}`}
                className={cn(
                  "text-xs font-semibold px-3 py-1.5 rounded-md max-w-full",
                  transport.className,
                )}
              >
                {transport.label}
              </Badge>
            )}
            {isNewAgent(agent) && (
              <Badge variant="outline" className="text-xs bg-blue-500/15 text-blue-600 border-blue-500/30 font-medium max-w-full">
                NEW
              </Badge>
            )}
            {/* Event-coord PR-3: in-flight wait_for_events indicator.
                `wait_for_events_in_flight` is sourced from /api/all-data,
                which snapshots `g.lock_for(agent_id).locked()` server-side
                (the PR-2 per-agent serialization lock). Hidden when
                FALSE / absent so the row stays uncluttered when the
                agent isn't auto-looping. */}
            {agent.wait_for_events_in_flight && (
              <Badge
                variant="outline"
                className="text-xs bg-sky-500/15 text-sky-600 border-sky-500/30 font-medium max-w-full"
                title="Agent is in a wait_for_events long-poll (auto event-loop)"
              >
                WAITING
              </Badge>
            )}
            {/* Operator-paused: auto_event_loop is OFF (Disconnect). The
                agent has been told to stop its monitoring loop; resume with
                Reconnect. Authoritative + immediate — shown even if the
                online dot lingers briefly on the presence grace window. */}
            {agent.status !== 'terminated' && agent.auto_event_loop === false && (
              <Badge
                variant="outline"
                className="text-xs bg-amber-500/15 text-amber-600 border-amber-500/30 font-medium max-w-full"
                title="Disconnected by operator — monitoring paused. Reconnect to resume."
              >
                PAUSED
              </Badge>
            )}
          </div>
        )
      },
    },
    {
      id: 'tasks',
      header: 'Tasks',
      // Trimmed w-64 → w-56 to pay for the STATUS and ACTIONS widening
      // above. Under `table-fixed` the AGENT column is the only elastic
      // one, and it holds the row's identity (a full agent_id); 32px
      // back here keeps it ~200px instead of ~170px. Task titles
      // truncate either way.
      headClassName: 'w-56',
      cellClassName: 'py-3 max-w-xs',
      cell: (agent) => <AgentTasksCell agent={agent} onTaskClick={onTaskClick} />,
    },
    {
      id: 'token',
      header: 'Token',
      headClassName: 'w-36',
      cellClassName: 'py-3',
      cell: (agent) => <AgentTokenCell agent={agent} />,
    },
    {
      id: 'actions',
      header: 'Actions',
      // w-36's 128px content box could not hold the five-button live
      // toolbar (5 × 28px + 4 × 4px = 156px) — measured 20px past the
      // cell edge on every live row. w-44 fits it; the terminated
      // variant (text Restore / Purge buttons) still exceeds it and
      // falls back to the cell's `flex-wrap`.
      headClassName: 'w-44',
      cellClassName: 'py-3',
      // Row-action buttons. Every onClick must stopPropagation —
      // otherwise the row-body onClick (which opens View) fires on
      // top of the destructive Terminate / Purge confirm.
      cell: (agent) => (
        <div className="flex flex-wrap items-center justify-end gap-1 min-w-0 opacity-0 group-hover:opacity-100 transition-opacity">
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); openView(agent) }}
            title="View details"
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
          >
            <Eye className="h-3.5 w-3.5" />
          </Button>
          {/* Send directive (ad-hoc poke). Agent-centric action — works
              for ANY agent regardless of whether it has a schedule.
              Live agents only (poking a terminated agent 404s); Admin
              excluded (the operator pseudo-agent never calls
              wait_for_events). */}
          {agent.status !== 'terminated' && agent.agent_id !== 'Admin' && (
            <Button
              variant="ghost"
              size="sm"
              onClick={(e) => { e.stopPropagation(); onSendDirective(agent.agent_id) }}
              title="Send directive (deliver now if listening, else queue)"
              className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
              data-testid={`send-directive-${agent.agent_id}`}
            >
              <Send className="h-3.5 w-3.5" />
            </Button>
          )}
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
          {/* Disconnect (pause monitoring) / Reconnect (resume). Reversible;
              does NOT terminate or revoke the token. Shows Reconnect when the
              agent is already paused (auto_event_loop === false), else the
              Disconnect (pause) affordance. */}
          {agent.status !== 'terminated' && agent.agent_id !== 'Admin' && (
            agent.auto_event_loop === false ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => { e.stopPropagation(); onReconnect(agent.agent_id) }}
                title="Reconnect (resume monitoring — re-enable the event loop)"
                className="h-7 w-7 p-0 text-primary hover:text-primary/80 hover:bg-primary/10"
                data-testid={`reconnect-${agent.agent_id}`}
              >
                <Play className="h-3.5 w-3.5" />
              </Button>
            ) : (
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => { e.stopPropagation(); onDisconnect(agent.agent_id) }}
                title="Disconnect (pause monitoring now; tells the agent to stop listening — resume anytime)"
                className="h-7 w-7 p-0 text-muted-foreground hover:text-amber-500 hover:bg-amber-500/10"
                data-testid={`disconnect-${agent.agent_id}`}
              >
                <Pause className="h-3.5 w-3.5" />
              </Button>
            )
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
      ),
    },
  ], [
    onTerminate, onRestore, onPurge, openView, onEdit, onTaskClick,
    onSendDirective, onDisconnect, onReconnect,
  ])
}
