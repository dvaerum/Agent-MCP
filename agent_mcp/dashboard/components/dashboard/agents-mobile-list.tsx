"use client"

import * as React from "react"
import {
  Eye, Pencil, Trash2, RotateCcw, Copy, Send, Pause, Play,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { agentPresence, type Agent } from "@/lib/api"
import { transportStatusBadge } from "@/lib/status"
import {
  PRESENCE_LABEL,
  PRESENCE_TONE,
} from "@/components/dashboard/agents/agent-presence"

/**
 * Mobile card rendering of a single agent row (CC-7 audit 2026-06-02).
 *
 * Desktop columns (Agent / Status / Tasks / Token / Actions) become a
 * stacked card: agent_id + status badge top, token snippet middle,
 * action row at the bottom with h-9 w-9 touch targets (CC-12).
 *
 * Action rules mirror the desktop column spec exactly: Admin can't be
 * edited / terminated / restored / purged; non-terminated agents show
 * Edit + Disconnect/Reconnect + Terminate; terminated agents show
 * Restore + Purge.
 *
 * This is a *single card* (`<li>`); the `<ul>` wrapper is provided by
 * <ResponsiveDataTable>'s `renderMobileCard` slot. Pre-foundation this
 * file exported a whole-list `<AgentsMobileList>` — that role now
 * belongs to the shared scaffold, leaving only the per-row markup here.
 * The presence label/tone tables it used to keep "in sync with the
 * desktop" by hand now come from `agents/agent-presence.tsx`.
 */

interface AgentMobileCardProps {
  agent: Agent
  openView: (agent: Agent) => void
  onEdit: (agent: Agent) => void
  onTerminate: (agentId: string) => void
  onRestore: (agentId: string) => void
  onPurge: (agentId: string) => void
  onSendDirective: (agentId: string) => void
  onDisconnect: (agentId: string) => void
  onReconnect: (agentId: string) => void
}

export function AgentMobileCard({
  agent,
  openView,
  onEdit,
  onTerminate,
  onRestore,
  onPurge,
  onSendDirective,
  onDisconnect,
  onReconnect,
}: AgentMobileCardProps): React.ReactElement {
  const isAdmin = agent.agent_id === "Admin"
  const isTerminated = agent.status === "terminated"
  const presence = agentPresence(agent)
  // ADR-0021 — delivery transport liveness (distinct from presence);
  // null when no delivery transport has reported for this agent.
  const transport = transportStatusBadge(agent.transport_status)
  return (
    <li
      onClick={() => openView(agent)}
      className="p-4 hover:bg-muted/30 active:bg-muted/50 transition-colors duration-150 cursor-pointer"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          {/* Same treatment as the desktop AGENT cell (see
              `agent-columns.tsx`): the `#{slice(-6)}` line under this
              one repeated the tail of the id above it while the id
              itself was clipped mid-`@host`, which is the part that
              tells two agents apart. The id gets both lines;
              `line-clamp-2` caps the card height and `title` keeps the
              full value reachable — it had none before, so a clipped
              id was simply unrecoverable here. */}
          <div
            className="font-medium text-sm text-foreground break-words line-clamp-2"
            title={agent.agent_id}
          >
            {agent.agent_id}
          </div>
        </div>
        <Badge
          variant="outline"
          className={cn(
            "shrink-0 text-[10px] font-semibold border-0 px-2 py-0.5 rounded-md uppercase tracking-wider",
            PRESENCE_TONE[presence],
          )}
        >
          {PRESENCE_LABEL[presence]}
        </Badge>
        {/* ADR-0021: delivery transport_status — distinct axis from
            the presence badge; rendered only when reported. */}
        {transport && (
          <Badge
            variant="outline"
            title={`Delivery transport: ${transport.label.toLowerCase()}`}
            className={cn(
              "shrink-0 text-[10px] font-semibold px-2 py-0.5 rounded-md uppercase tracking-wider",
              transport.className,
            )}
          >
            {transport.label}
          </Badge>
        )}
        {!isTerminated && agent.auto_event_loop === false && (
          <Badge
            variant="outline"
            className="shrink-0 text-[10px] font-semibold border-0 px-2 py-0.5 rounded-md uppercase tracking-wider bg-amber-500/15 text-amber-600 ring-1 ring-amber-500/30"
            title="Disconnected by operator — monitoring paused. Reconnect to resume."
          >
            PAUSED
          </Badge>
        )}
      </div>

      {agent.auth_token && (
        <div className="flex items-center gap-2 mt-3">
          <code className="text-[11px] font-mono text-muted-foreground truncate">
            {agent.auth_token.slice(0, 12)}…
          </code>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 w-7 p-0"
            onClick={(e) => {
              e.stopPropagation()
              navigator.clipboard.writeText(agent.auth_token || "")
            }}
            aria-label="Copy auth token"
            title="Copy auth token"
          >
            <Copy className="h-3 w-3" />
          </Button>
        </div>
      )}

      <div className="flex items-center justify-end gap-1 mt-3">
        <Button
          variant="ghost"
          size="sm"
          title="View details"
          aria-label="View details"
          className="h-9 w-9 p-0 text-muted-foreground hover:text-foreground"
          onClick={(e) => { e.stopPropagation(); openView(agent) }}
        >
          <Eye className="h-4 w-4" />
        </Button>
        {!isAdmin && !isTerminated && (
          <>
            <Button
              variant="ghost"
              size="sm"
              title="Send directive (deliver now if listening, else queue)"
              aria-label="Send directive"
              className="h-9 w-9 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
              onClick={(e) => { e.stopPropagation(); onSendDirective(agent.agent_id) }}
              data-testid={`send-directive-mobile-${agent.agent_id}`}
            >
              <Send className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              title="Edit agent"
              aria-label="Edit agent"
              className="h-9 w-9 p-0 text-primary hover:text-primary hover:bg-primary/10"
              onClick={(e) => { e.stopPropagation(); onEdit(agent) }}
            >
              <Pencil className="h-4 w-4" />
            </Button>
            {agent.auto_event_loop === false ? (
              <Button
                variant="ghost"
                size="sm"
                title="Reconnect (resume monitoring)"
                aria-label="Reconnect agent"
                className="h-9 w-9 p-0 text-primary hover:text-primary hover:bg-primary/10"
                onClick={(e) => { e.stopPropagation(); onReconnect(agent.agent_id) }}
                data-testid={`reconnect-mobile-${agent.agent_id}`}
              >
                <Play className="h-4 w-4" />
              </Button>
            ) : (
              <Button
                variant="ghost"
                size="sm"
                title="Disconnect (pause monitoring; resume anytime)"
                aria-label="Disconnect agent"
                className="h-9 w-9 p-0 text-muted-foreground hover:text-amber-600 hover:bg-amber-500/10"
                onClick={(e) => { e.stopPropagation(); onDisconnect(agent.agent_id) }}
                data-testid={`disconnect-mobile-${agent.agent_id}`}
              >
                <Pause className="h-4 w-4" />
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              title="Terminate (soft-delete; can be restored or purged)"
              aria-label="Terminate agent"
              className="h-9 w-9 p-0 text-destructive hover:text-destructive hover:bg-destructive/10"
              onClick={(e) => { e.stopPropagation(); onTerminate(agent.agent_id) }}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </>
        )}
        {!isAdmin && isTerminated && (
          <>
            <Button
              variant="ghost"
              size="sm"
              title="Restore"
              aria-label="Restore agent"
              className="h-9 px-3 text-xs text-primary hover:text-primary hover:bg-primary/10"
              onClick={(e) => { e.stopPropagation(); onRestore(agent.agent_id) }}
            >
              <RotateCcw className="h-3.5 w-3.5 mr-1" />
              Restore
            </Button>
            <Button
              variant="ghost"
              size="sm"
              title="Purge"
              aria-label="Purge agent"
              className="h-9 px-3 text-xs text-destructive hover:text-destructive hover:bg-destructive/10"
              onClick={(e) => { e.stopPropagation(); onPurge(agent.agent_id) }}
            >
              <Trash2 className="h-3.5 w-3.5 mr-1" />
              Purge
            </Button>
          </>
        )}
      </div>
    </li>
  )
}
