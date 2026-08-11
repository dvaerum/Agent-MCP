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
import { useAgentTasks } from "@/lib/queries/all-data"
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

/**
 * Class list for the Agents `<table>` itself.
 *
 * Lives here, next to the column widths it depends on, because the two
 * are one decision: `min-w` must be at least the sum of the fixed
 * columns plus a usable floor for the elastic one, so a change to
 * either belongs in the same edit.
 *
 * `table-fixed` — a pathologically long value in ANY cell (a 5000-char
 * agent name was the live case, PR #588's predecessor) stretches an
 * auto-layout table thousands of px wide and pushes every other column
 * off-screen; measured 40,660px → 923px when this was added. Fixed
 * layout also makes the widths data-INdependent, so the columns don't
 * shuffle as rows come and go.
 *
 * `min-w-[56rem]` (896px) — the missing half of that decision. Under
 * fixed layout the elastic column absorbs the leftover, and when the
 * container is narrower than the fixed columns' sum that leftover goes
 * NEGATIVE: the browser clamps the column to 0px and its content paints
 * over the next one. Measured on a 1024×800 viewport (677px container,
 * over which the old columns' 720px sum forced the table wider and left
 * the elastic one nothing): AGENT 0px wide, 36 elements painting
 * outside their own `<td>` — the very defect #595 fixed, one
 * breakpoint down, because the wrapper had nothing to scroll.
 * The floor keeps every column at its minimum and lets the wrapper's
 * `overflow-x-auto` scroll instead — 688px of fixed columns + a 208px
 * AGENT floor. It sits below the 923px container a 1280 viewport gives,
 * so it costs nothing at the widths that already fit.
 */
export const AGENTS_TABLE_CLASS = "table-fixed min-w-[56rem]"

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
  const agentTasks = useAgentTasks(agent.agent_id)
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
      // NO width class, deliberately: AGENT is the table's single
      // elastic column. Every other column reserves its measured
      // content minimum and this one takes whatever is left, which is
      // the right way round — it carries the row's primary key, and
      // it's also the only cell whose content degrades gracefully
      // (text clips) rather than painting outside its box (badges and
      // icon buttons are fixed-size and `shrink-0`).
      //
      // `whitespace-normal` undoes the `whitespace-nowrap` that shadcn's
      // <TableCell> puts on every <td>. It's the reason this cell could
      // only ever truncate: under `white-space: nowrap` the id's
      // `break-words` is inert and the text stays on one line no matter
      // how much room the box has. Scoped to this column — the other
      // four WANT nowrap (badges and buttons must not break up).
      cellClassName: 'py-3 whitespace-normal',
      cell: (agent) => (
        <div className="flex items-center gap-3">
          <StatusDot presence={agentPresence(agent)} />
          <AgentTypeIcon agentId={agent.agent_id} />
          <div className="min-w-0 flex-1">
            {/* Two lines, both spent on the id.
                The line under this one used to render
                `#{agent_id.slice(-6)}` — the last six characters of the
                string directly above it. That is no information at all,
                and on the live fleet it's actively ambiguous: both
                `pikvm-nixos@nixos-developer-system` and
                `pikvm-mcp-server@nixos-developer-system` rendered
                `#system`. Meanwhile the id itself was clipped at 17 of
                39 characters, losing the `@host` suffix that's the only
                thing telling those two agents apart.

                So the id gets the line back. `break-words` because an
                agent id has no spaces — without it the second line
                would never be reached. `line-clamp-2` caps the height
                (a 5000-char id must not grow the row) and brings
                `overflow:hidden`, so this stays inside the cell no
                matter how long the id is; `title` keeps the full value
                reachable in the cases that still clip. */}
            <div
              className="font-medium text-sm text-foreground break-words line-clamp-2"
              title={agent.agent_id}
            >
              {agent.agent_id}
            </div>
          </div>
        </div>
      ),
    },
    {
      id: 'status',
      header: 'Status',
      // The table is `table-fixed` (see AGENTS_TABLE_CLASS), so this
      // width is the cell's HARD box — content that can't shrink or
      // wrap paints straight over the next column. w-32 (112px content
      // box) could not hold even one badge PAIR: measured live, an
      // ONLINE + WAITING row overflowed 29px into TASKS and an
      // ONLINE + WORKING + WAITING row overflowed 121px.
      //
      // w-44 is the EXACT minimum, not a round number: re-measured per
      // badge in Firefox, ONLINE is 70.2px and WORKING 84.9px, so the
      // common presence+transport pair is 70.2 + 4 (gap-1) + 84.9 =
      // 159.1px against w-44's 160px content box — 0.9px to spare.
      // w-40 (144px) would break the pair; nothing here can be given
      // back to AGENT.
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
      // w-52 (208px, content box 192px). The widest thing that MUST fit
      // is the summary sub-line — "18 assigned, 3 contributed" measures
      // ~132px; titles clip at any width, so the floor is set by the
      // sub-line, not by them. That's 16px back from w-56, and it goes
      // to AGENT.
      //
      // This column was the one candidate for proportional `minmax`
      // sizing — a flat reserve is arguably wrong at the wide end,
      // where the table is 1483px at 1920 and TASKS sits at 208px
      // while AGENT balloons to ~750px for an id needing 293px.
      // `w-[max(13rem,18%)]` expresses exactly that. It does not work:
      // probed in Firefox on the live table (table width 922.8px),
      // setting this column's width to
      //     13rem            → 208.0px  ✓
      //     18%              → 166.1px  ✓ (resolved against the table)
      //     calc(18%)        → 166.1px  ✓
      //     max(13rem,18%)   → 221.4px  ✗ — the even auto split
      // i.e. the fixed-table-layout column algorithm DROPS a `max()`
      // mixing a length and a percentage and falls back to auto, which
      // hands the column a share of the slack instead of a floor —
      // the opposite of the intent, and silently. A plain percentage
      // works but has no floor, so it starves this column at 1280 (the
      // width that actually hurts) to pad it at 1920 (where nothing is
      // starving). Fixed px is the honest answer until table columns
      // can express minmax.
      headClassName: 'w-52',
      cellClassName: 'py-3 max-w-xs',
      cell: (agent) => <AgentTasksCell agent={agent} onTaskClick={onTaskClick} />,
    },
    {
      id: 'token',
      header: 'Token',
      // Trimmed w-36 → w-32. Measured live, the cell's intrinsic width
      // is 97px: the 8-char elision is ≤65px, plus gap-2 (8px) and the
      // 24px copy button. w-36's 128px content box was reserving 31px
      // that nothing ever used, at the direct expense of AGENT. w-32
      // (112px) still clears it by 15px, and the transient "copied"
      // flash is absorbed by the `min-w-0` on the <code> as before.
      // w-28 (96px) would be 1px short.
      //
      // Considered and rejected: dropping this column entirely at
      // narrow widths via `hideBelow`. It's the least valuable column
      // (an opaque 8-char elision behind a copy button, with the full
      // token in the detail dialog) — but the breakpoints are VIEWPORT
      // -keyed while the constraint is the table's CONTAINER (923px
      // inside a 1280 viewport, because of the sidebar), so no
      // breakpoint lines up with the width that actually hurts. And
      // once the id gets its second line back it no longer needs the
      // 128px, so the cost would buy nothing.
      headClassName: 'w-32',
      cellClassName: 'py-3',
      cell: (agent) => <AgentTokenCell agent={agent} />,
    },
    {
      id: 'actions',
      header: 'Actions',
      // w-36's 128px content box could not hold the five-button live
      // toolbar (5 × 28px + 4 × 4px = 156px) — measured 20px past the
      // cell edge on every live row. w-44's 160px box fits it with 4px
      // to spare, so like STATUS this is a measured minimum with
      // nothing to give back; the terminated variant (text Restore /
      // Purge buttons) still exceeds it and falls back to the cell's
      // `flex-wrap`.
      headClassName: 'w-44',
      cellClassName: 'py-3',
      // Row-action buttons. Every onClick must stopPropagation —
      // otherwise the row-body onClick (which opens View) fires on
      // top of the destructive Terminate / Purge confirm.
      cell: (agent) => (
        <div className="flex flex-wrap items-center gap-1 min-w-0 opacity-0 group-hover:opacity-100 transition-opacity">
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
