"use client"

import * as React from "react"
import type { LucideIcon } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * Shared empty-state primitive used by every list/table dashboard.
 *
 * Pre-audit: each dashboard rolled its own empty-state markup —
 * tasks-dashboard used the slate/teal palette anti-pattern (CC-1),
 * agents-dashboard used semantic tokens, memories-dashboard used
 * yet another spacing scale, and messages-dashboard had no empty
 * state at all (CC-20). Centralising here gets all five pages
 * on the same modern-minimal pattern (single icon, single title,
 * single description line, optional CTA), eliminates the CC-1
 * offender, and gives the messages page a real "no rows" body.
 *
 * Layout is `flex-col items-center justify-center text-center`
 * with generous vertical padding so it reads as a deliberate
 * "nothing here yet" rather than as a layout bug.
 */
export interface EmptyStateProps {
  /** Lucide icon component — passed as a component reference, not JSX. */
  icon: LucideIcon
  /** Short heading. Sentence case, no terminal punctuation. */
  title: string
  /** Optional secondary line. Explains *why* the list is empty. */
  description?: string
  /** Optional CTA — usually a `<Button>` or modal-trigger. */
  action?: React.ReactNode
  /** Override the wrapper padding for unusual contexts. */
  className?: string
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps): React.ReactElement {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        "flex flex-col items-center justify-center text-center",
        "px-6 py-12 sm:py-16",
        className,
      )}
    >
      <Icon
        aria-hidden
        className="h-10 w-10 text-muted-foreground/60 mb-4"
      />
      <h3 className="text-base sm:text-lg font-medium text-foreground mb-1">
        {title}
      </h3>
      {description && (
        <p className="max-w-sm text-sm text-muted-foreground">
          {description}
        </p>
      )}
      {action && <div className="mt-6">{action}</div>}
    </div>
  )
}
