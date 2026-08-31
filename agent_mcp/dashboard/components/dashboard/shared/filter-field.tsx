"use client"

import * as React from "react"
import { cn } from "@/lib/utils"

/**
 * A filter/sort control with a small visible label stacked above it, so
 * the control is self-describing both BEFORE it's opened and AFTER a
 * value is picked.
 *
 * Extracted from messages-dashboard.tsx (the one page that had already
 * solved this) during the filter-bar audit sweep — every other list
 * page's filter bar had bare `<Select>`/`<AgentSelect>` controls with no
 * visible label and, in most cases, no `aria-label` either: pick "High"
 * on the Tasks page and nothing on screen says whether that's a status,
 * an assignment, or a priority. The visible label here and an
 * `aria-label`/`ariaLabel` on the control itself are BOTH still needed —
 * this `<span>` is decorative only (no `aria-labelledby` wiring to the
 * control), so it helps sighted users but does nothing for a screen
 * reader on its own.
 */
export function FilterField({
  label,
  className,
  children,
}: {
  label: string
  className?: string
  children: React.ReactNode
}): React.ReactElement {
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      {children}
    </div>
  )
}
