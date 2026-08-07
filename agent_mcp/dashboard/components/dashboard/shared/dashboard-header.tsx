"use client"

import * as React from "react"
import { RefreshCw } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * The dashboard "parity standard" page header, extracted from the
 * ~30-line block that was copy-pasted verbatim across
 * memories/settings/messages/agents/tasks (each copy self-documented
 * with a `// matches …` comment — see the architecture review, Class 2).
 *
 * Layout (matches the pre-extraction copies exactly):
 *   left:  <h1> title + optional subtitle <p>
 *   right: project chip (Badge + static dot) → "Last updated: …" →
 *          Refresh button → caller-supplied actions slot
 *
 * The project chip renders only when `serverName` is set; "Last
 * updated" only when `lastUpdated` is set; Refresh only when
 * `onRefresh` is set — so a page opts into each affordance by simply
 * passing (or omitting) the corresponding prop.
 */
export interface DashboardHeaderProps {
  /** Page title, e.g. "Memory Bank". */
  title: string
  /** Optional one-line subtitle under the title. */
  subtitle?: string
  /** Project/server chip label. Omit to hide the chip. */
  serverName?: string
  /**
   * Last-refresh time — a Date-parseable string or epoch ms. Rendered
   * as `Last updated: <localized time>`. Omit to hide.
   */
  lastUpdated?: number | string
  /** Refresh handler. Omit to hide the Refresh button. */
  onRefresh?: () => void
  /** Spins the icon and disables the button while a refresh is inflight. */
  refreshing?: boolean
  /** Page-level actions (e.g. a Create modal trigger). */
  actions?: React.ReactNode
}

export function DashboardHeader({
  title,
  subtitle,
  serverName,
  lastUpdated,
  onRefresh,
  refreshing,
  actions,
}: DashboardHeaderProps): React.ReactElement {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
      <div>
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-foreground">
          {title}
        </h1>
        {subtitle && (
          <p className="text-muted-foreground text-sm sm:text-base mt-1">
            {subtitle}
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3">
        {serverName && (
          // CC-19: static server-online dot (no animate-pulse).
          <Badge
            variant="outline"
            className="text-xs bg-primary/15 text-primary border-primary/30 font-medium"
          >
            <span aria-hidden className="w-2 h-2 bg-primary rounded-full mr-2" />
            {serverName}
          </Badge>
        )}
        {lastUpdated !== undefined && lastUpdated !== null && (
          <span className="text-xs text-muted-foreground">
            Last updated: {new Date(lastUpdated).toLocaleTimeString()}
          </span>
        )}
        {onRefresh && (
          <Button
            variant="outline"
            size="sm"
            onClick={onRefresh}
            disabled={refreshing}
            className="text-xs"
          >
            <RefreshCw
              className={cn("h-3.5 w-3.5 mr-1.5", refreshing && "animate-spin")}
            />
            Refresh
          </Button>
        )}
        {actions}
      </div>
    </div>
  )
}
