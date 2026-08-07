"use client"

import * as React from "react"
import { Shield, Cpu, Database, Terminal } from "lucide-react"
import { cn } from "@/lib/utils"
import type { AgentPresence } from "@/lib/api"

/**
 * Presence vocabulary shared by the Agents desktop columns, the mobile
 * card, and the detail dialog.
 *
 * Pre-extraction `PRESENCE_LABEL` existed TWICE — once in
 * `agents-dashboard.tsx` and once in `agents-mobile-list.tsx` — with a
 * `// kept in sync with the desktop PRESENCE_LABEL` comment standing in
 * for a shared module (architecture review, Class 6: the mobile twin
 * drifts because parity is enforced socially). One owner now; both
 * surfaces import it.
 *
 * Display label per presence state. `pending` (registered but never
 * connected) and `offline` (was connected, now down) both read as
 * "OFFLINE" — the distinction wasn't worth two look-alike badges, so it
 * collapses in the badge and dot; the never-connected case still gets its
 * own tooltip ("paste the snippet …") for the operator who hovers. The
 * `pending` KEY stays in the logic so that tooltip branch survives.
 */
export const PRESENCE_LABEL: Record<AgentPresence, string> = {
  online: "ONLINE",
  pending: "OFFLINE",
  offline: "OFFLINE",
  terminated: "TERMINATED",
}

/** Desktop badge tone (px-3 py-1.5 pill on the agents table). */
export const PRESENCE_BADGE_CLASS: Record<AgentPresence, string> = {
  online: "bg-primary/15 text-primary ring-1 ring-primary/20",
  // pending collapses into offline's look (see PRESENCE_LABEL).
  pending: "bg-muted/50 text-muted-foreground ring-1 ring-border",
  offline: "bg-muted/50 text-muted-foreground ring-1 ring-border",
  terminated: "bg-muted/50 text-muted-foreground ring-1 ring-border",
}

/** Mobile card badge tone (denser pill; `bg-muted` rather than /50). */
export const PRESENCE_TONE: Record<AgentPresence, string> = {
  online: "bg-primary/15 text-primary ring-1 ring-primary/20",
  pending: "bg-muted text-muted-foreground ring-1 ring-border",
  offline: "bg-muted text-muted-foreground ring-1 ring-border",
  terminated: "bg-muted text-muted-foreground ring-1 ring-border",
}

/** Hover rationale for the presence badge. */
export function presenceTitle(
  presence: AgentPresence,
  lastMcpConnection?: string | null,
): string {
  switch (presence) {
    case "pending":
      return "Registered but no MCP session yet. Paste the snippet into the user’s claude .mcp.json to bring this agent online."
    case "offline":
      return `No live MCP stream. Last seen: ${lastMcpConnection ?? "unknown"}`
    case "online":
      return "Live MCP stream attached."
    default:
      return "Soft-deleted via the Terminate action. Restore or Purge from the row actions."
  }
}

// Wave 7 PR 2: presence-driven dot. The legacy `Agent['status']`
// values (running / pending / terminated / failed) were spawn-
// lifecycle artefacts. The coordinator model surfaces live MCP
// presence instead: see `agentPresence()` in `lib/api.ts`.
export const StatusDot = React.memo(function StatusDot({
  presence,
}: {
  presence: AgentPresence
}) {
  const config: Record<AgentPresence, string> = {
    online: "bg-primary shadow-primary/50 shadow-md",
    // pending collapses into offline's look (see PRESENCE_LABEL).
    pending: "bg-muted-foreground shadow-muted-foreground/50 shadow-md",
    offline: "bg-muted-foreground shadow-muted-foreground/50 shadow-md",
    terminated: "bg-muted-foreground shadow-muted-foreground/50 shadow-md",
  }
  return <div className={cn("w-2.5 h-2.5 rounded-full", config[presence])} />
})

export const AgentTypeIcon = React.memo(function AgentTypeIcon({
  agentId,
}: {
  agentId: string
}) {
  const getIcon = () => {
    if (agentId.includes("admin")) return Shield
    if (agentId.includes("worker")) return Cpu
    if (agentId.includes("analysis")) return Database
    if (agentId.includes("security")) return Shield
    return Terminal
  }

  const Icon = getIcon()
  return <Icon className="h-4 w-4 text-muted-foreground" />
})
