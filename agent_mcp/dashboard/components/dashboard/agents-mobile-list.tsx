"use client"

import * as React from "react"
import {
  Eye, Pencil, Trash2, RotateCcw, Copy, Send,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { agentPresence, type Agent, type AgentPresence } from "@/lib/api"

/**
 * Mobile card-list rendering of the agents table (CC-7 audit 2026-06-02).
 *
 * Desktop columns (Agent / Status / Tasks / Token / Actions) become a
 * stacked card: agent_id + status badge top, token snippet middle,
 * action row at the bottom with h-9 w-9 touch targets (CC-12).
 *
 * Action rules mirror the desktop CompactAgentRow exactly: Admin can't
 * be edited / terminated / restored / purged; non-terminated agents
 * show Edit + Terminate; terminated agents show Restore + Purge.
 */

// Wave 7 PR 2 — presence-driven tone. Replaces the spawn-lifecycle
// `status` keys ('running' / 'pending' / 'terminated' / 'failed').
const PRESENCE_TONE: Record<AgentPresence, string> = {
  online: "bg-primary/15 text-primary ring-1 ring-primary/20",
  pending: "bg-amber-500/15 text-amber-600 dark:text-amber-400 ring-1 ring-amber-500/20",
  offline: "bg-muted text-muted-foreground ring-1 ring-border",
  terminated: "bg-muted text-muted-foreground ring-1 ring-border",
}

interface AgentsMobileListProps {
  agents: Agent[]
  openView: (agent: Agent) => void
  onEdit: (agent: Agent) => void
  onTerminate: (agentId: string) => void
  onRestore: (agentId: string) => void
  onPurge: (agentId: string) => void
  onSendDirective: (agentId: string) => void
}

export function AgentsMobileList({
  agents,
  openView,
  onEdit,
  onTerminate,
  onRestore,
  onPurge,
  onSendDirective,
}: AgentsMobileListProps): React.ReactElement {
  return (
    <ul role="list" className="divide-y divide-border">
      {agents.map((agent) => {
        const isAdmin = agent.agent_id === "Admin"
        const isTerminated = agent.status === "terminated"
        const presence = agentPresence(agent)
        return (
          <li
            key={agent.agent_id}
            onClick={() => openView(agent)}
            className="p-4 hover:bg-muted/30 active:bg-muted/50 transition-colors duration-150 cursor-pointer"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="font-medium text-sm text-foreground truncate">
                  {agent.agent_id}
                </div>
                <div className="text-[11px] text-muted-foreground font-mono tabular-nums mt-0.5">
                  #{agent.agent_id.slice(-6)}
                </div>
              </div>
              <Badge
                variant="outline"
                className={cn(
                  "shrink-0 text-[10px] font-semibold border-0 px-2 py-0.5 rounded-md uppercase tracking-wider",
                  PRESENCE_TONE[presence],
                )}
              >
                {presence}
              </Badge>
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
      })}
    </ul>
  )
}
