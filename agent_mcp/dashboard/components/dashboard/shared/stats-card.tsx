"use client"

import * as React from "react"
import { cn } from "@/lib/utils"

/**
 * Shared stat tile used by every list dashboard's stats strip.
 *
 * Pre-extraction this component was copy-pasted 4× — agents-dashboard,
 * tasks-dashboard, memories-dashboard, messages-dashboard — with the
 * identical prop shape, and it had already drifted:
 *
 *   - down-trend colour split: agents/memories/messages used the
 *     semantic `text-destructive`; tasks used a literal
 *     `text-orange-500`. Reconciled here to `text-destructive`.
 *   - only the tasks copy was wrapped in `React.memo`; the other three
 *     re-rendered on every parent render. Reconciled here to memoized.
 *   - the icon prop was typed `any` in tasks and
 *     `React.ComponentType<{className?}>` elsewhere. Reconciled to the
 *     typed component reference (no `any`).
 *
 * Visual contract (CC-4/CC-8/CC-16 audit): semantic tokens only
 * (`bg-card` / `border-border` / `text-foreground` /
 * `text-muted-foreground`), `rounded-lg`, plain Tailwind sizing,
 * `tabular-nums` on the numeral so digits don't shift width.
 */
export interface StatsCardProps {
  /** Icon component reference (e.g. a lucide-react icon), not JSX. */
  icon: React.ComponentType<{ className?: string }>
  /** Short uppercase label, e.g. "Total". */
  label: string
  /** The primary numeral. */
  value: number
  /** Optional secondary line under the value, e.g. "12 entries". */
  change?: string
  /** Tints the `change` line: up=emerald, down=destructive, neutral=muted. */
  trend?: "up" | "down" | "neutral"
  /** Extra classes for the outer card. */
  className?: string
}

export const StatsCard = React.memo(function StatsCard({
  icon: Icon,
  label,
  value,
  change,
  trend,
  className,
}: StatsCardProps) {
  return (
    <div
      data-slot="stats-card"
      className={cn(
        "bg-card border border-border rounded-lg p-3 sm:p-5",
        "hover:bg-muted/30 transition-colors duration-150 group",
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2 mb-2">
            <Icon className="h-4 w-4 text-muted-foreground transition-colors" />
            <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
              {label}
            </span>
          </div>
          <div className="text-2xl sm:text-3xl font-semibold text-foreground tabular-nums mb-1">
            {value}
          </div>
          {change && (
            <div
              className={cn(
                "text-xs font-medium tabular-nums",
                trend === "up" && "text-emerald-500",
                trend === "down" && "text-destructive",
                trend === "neutral" && "text-muted-foreground",
              )}
            >
              {change}
            </div>
          )}
        </div>
      </div>
    </div>
  )
})

StatsCard.displayName = "StatsCard"
