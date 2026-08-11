"use client"

import { Button } from "@/components/ui/button"

/**
 * v5.0.26 pagination footer — « Newest / Newer / Older / Oldest » plus
 * a "Showing N–M of T" range label.
 *
 * One button spec drives both layouts: a single justified row on sm+,
 * and (below sm) the range label stacked above a 4-column grid so the
 * labels stay readable at 375 px. Pre-scaffold these were two separate
 * hand-written copies — one in messages-dashboard.tsx, one inside
 * messages-mobile-list.tsx — which is the same double-renderer drift
 * the shared table retires.
 */
export function MessagesPagination({
  rangeStart,
  rangeEnd,
  total,
  onFirstPage,
  onLastPage,
  onNewest,
  onNewer,
  onOlder,
  onOldest,
}: {
  rangeStart: number
  rangeEnd: number
  total: number
  onFirstPage: boolean
  onLastPage: boolean
  onNewest: () => void
  onNewer: () => void
  onOlder: () => void
  onOldest: () => void
}) {
  const nav = [
    {
      key: "newest",
      label: "« Newest",
      onClick: onNewest,
      disabled: onFirstPage,
      ariaLabel: "jump to newest page",
    },
    { key: "newer", label: "Newer", onClick: onNewer, disabled: onFirstPage },
    { key: "older", label: "Older", onClick: onOlder, disabled: onLastPage },
    {
      key: "oldest",
      label: "Oldest »",
      onClick: onOldest,
      disabled: onLastPage,
      ariaLabel: "jump to oldest page",
    },
  ]
  const button = (b: (typeof nav)[number]) => (
    <Button
      key={b.key}
      variant="outline"
      size="sm"
      onClick={b.onClick}
      disabled={b.disabled}
      aria-label={b.ariaLabel}
    >
      {b.label}
    </Button>
  )
  const range = `Showing ${rangeStart}–${rangeEnd} of ${total}`
  return (
    <>
      <div className="hidden sm:flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">{nav.slice(0, 2).map(button)}</div>
        <div className="text-xs text-muted-foreground tabular-nums">{range}</div>
        <div className="flex items-center gap-2">{nav.slice(2).map(button)}</div>
      </div>
      <div className="block sm:hidden">
        <div className="text-[11px] text-muted-foreground tabular-nums text-center mb-2">
          {range}
        </div>
        <div className="grid grid-cols-4 gap-2">{nav.map(button)}</div>
      </div>
    </>
  )
}
