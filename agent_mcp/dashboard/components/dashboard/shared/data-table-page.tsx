"use client"

import * as React from "react"
import { AlertCircle, ShieldAlert, type LucideIcon } from "lucide-react"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import {
  DashboardHeader,
  type DashboardHeaderProps,
} from "@/components/dashboard/shared/dashboard-header"
import { StatsCard, type StatsCardProps } from "@/components/dashboard/shared/stats-card"
import {
  EmptyState,
  type EmptyStateProps,
} from "@/components/dashboard/shared/empty-state"
import {
  ResponsiveDataTable,
  type Column,
} from "@/components/dashboard/shared/responsive-data-table"

/**
 * The list-page scaffold — the keystone extraction of this refactor.
 *
 * Every `*-dashboard.tsx` list page hand-rolled the SAME presentation
 * shell: header + stats strip + filter bar + loading skeleton + empty
 * state + error panel + forbidden panel + desktop table + mobile list.
 * Because there was no shared owner, a fix on page A never reached page
 * B — the recurring-bug mechanism the architecture review documents
 * (Classes 2/3/4, and the list-error half of Class 1).
 *
 * `<DataTablePage>` OWNS that shell. It builds ON TOP OF the existing
 * infra hooks — it does not re-implement fetch/401/403/abort logic. A
 * page picks a data source (`usePagedQuery` / `useRouterQuery` / a
 * store selector), maps its `{data, loading, error, forbidden}` result
 * into these props, and supplies a `Column<T>[]`. Everything else is
 * rendered here, once, for all pages.
 *
 * Render precedence (early-returns, matching the pre-extraction pages):
 *   1. `guard`     — a hard precondition panel (e.g. "No Server
 *                    Connection"); short-circuits everything.
 *   2. `loading` & no rows — the stats+table skeleton.
 *   3. `forbidden` — the centralized "Sysadmin only" panel (was
 *                    copy-pasted across 3 router views).
 *   4. `error` & no rows — the "Connection Error" panel.
 *   5. loaded      — header + stats + filter bar + card
 *                    (EmptyState when `rows` is empty, else the
 *                    responsive table) + `children` (modals), plus the
 *                    stale-data notice when `error` arrived with rows.
 *
 * Content in hand beats a transient failure, for BOTH the in-flight and
 * the failed case: a refresh (`loading`) with rows already present keeps
 * showing them rather than flashing the skeleton, and a *failed* refresh
 * (`error`) with rows already present keeps showing them rather than
 * blanking the page behind a full-page panel. These pages poll —
 * Messages every 60 s and on every SSE tick, Tasks every 60 s — so a
 * single dropped request would otherwise wipe a page the operator is
 * actively reading. The full panel is reserved for the first load, where
 * there is genuinely nothing else to render.
 *
 * `forbidden` deliberately still outranks `error` in both cases: a 403
 * is a standing authorization verdict, not a blip, and stale rows must
 * not survive it.
 */
export interface DataTablePageProps<T> {
  /** Header config (title/subtitle/chip/last-updated/refresh/actions). */
  header: DashboardHeaderProps
  /** Stats strip. Omit for no stats. */
  stats?: StatsCardProps[]
  /** Filter/search controls rendered under the stats strip. */
  filterBar?: React.ReactNode

  /** True while the data source is fetching. */
  loading: boolean
  /**
   * List-load error message, or null/undefined when healthy. Renders the
   * full-page "Connection Error" panel only when there are no rows to
   * fall back on; with rows present it degrades to the inline
   * stale-data notice (see the precedence note above).
   */
  error?: string | null
  /** 403 — renders the "Sysadmin only" panel. */
  forbidden?: boolean
  /**
   * Hard precondition panel (e.g. no server connection). When set,
   * short-circuits every other state.
   */
  guard?: { icon: LucideIcon; title: string; description?: string } | null

  /** Column spec for the responsive table. */
  columns: Column<T>[]
  /** Row data (post-filter/sort — the page owns filtering). */
  rows: T[]
  /** Stable identity per row. */
  getRowId: (row: T) => string
  /** Row click (opens a detail dialog, typically). */
  onRowClick?: (row: T) => void
  /** Custom mobile card; omit to auto-stack columns. */
  renderMobileCard?: (row: T) => React.ReactNode
  /**
   * Extra classes on data rows (static or per-row callback). See
   * `ResponsiveDataTableProps.rowClassName`.
   */
  rowClassName?: string | ((row: T) => string | undefined)
  /**
   * Expandable per-row detail (accordion pages, e.g. Groups). See
   * `ResponsiveDataTableProps.renderExpanded`.
   */
  renderExpanded?: (row: T) => React.ReactNode
  /** Extra classes on the desktop `<table>` (e.g. `table-fixed`). */
  tableClassName?: string

  /** Empty-state content when `rows` is empty. */
  empty: EmptyStateProps

  /** Skeleton row count (default 5). */
  skeletonRows?: number
  /** Skeleton stat-card count (defaults to `stats?.length ?? 4`). */
  skeletonStats?: number

  /** Rendered after the table card — typically the page's modals. */
  children?: React.ReactNode
}

function CenteredPanel({
  icon: Icon,
  iconClassName,
  title,
  description,
  descriptionClassName,
}: {
  icon: LucideIcon
  iconClassName?: string
  title: string
  description?: string
  descriptionClassName?: string
}) {
  return (
    <div className="h-full flex items-center justify-center">
      <div className="text-center space-y-4">
        <Icon className={cn("h-12 w-12 mx-auto", iconClassName)} />
        <div>
          <h3 className="text-lg font-medium text-foreground mb-2">{title}</h3>
          {description && (
            <p
              className={cn(
                "text-sm",
                descriptionClassName ?? "text-muted-foreground",
              )}
            >
              {description}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}

export function DataTablePage<T>({
  header,
  stats,
  filterBar,
  loading,
  error,
  forbidden,
  guard,
  columns,
  rows,
  getRowId,
  onRowClick,
  renderMobileCard,
  rowClassName,
  renderExpanded,
  tableClassName,
  empty,
  skeletonRows = 5,
  skeletonStats,
  children,
}: DataTablePageProps<T>): React.ReactElement {
  // 1. Hard precondition guard.
  if (guard) {
    return (
      <CenteredPanel
        icon={guard.icon}
        iconClassName="text-muted-foreground"
        title={guard.title}
        description={guard.description}
      />
    )
  }

  // 2. First-load skeleton (background refresh keeps content — below).
  if (loading && rows.length === 0) {
    const statCount = skeletonStats ?? stats?.length ?? 4
    return (
      <div
        data-slot="table-skeleton"
        className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6"
      >
        <div className="grid gap-3 sm:gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: statCount }).map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <Skeleton className="h-10 w-full sm:max-w-md" />
        <div className="bg-card border border-border rounded-lg p-4 space-y-3">
          {Array.from({ length: skeletonRows }).map((_, i) => (
            <Skeleton key={i} className="h-14 w-full" />
          ))}
        </div>
      </div>
    )
  }

  // 3. Forbidden (403) — the centralized "Sysadmin only" panel.
  if (forbidden) {
    return (
      <CenteredPanel
        icon={ShieldAlert}
        iconClassName="text-muted-foreground"
        title="Sysadmin only"
        description="You need sysadmin privileges to view this page."
      />
    )
  }

  // 4. First-load error — nothing in hand, so the panel owns the page.
  //    With rows present we fall through to the loaded branch and show
  //    the inline stale notice instead (mirrors the loading precedence).
  if (error && rows.length === 0) {
    return (
      <CenteredPanel
        icon={AlertCircle}
        iconClassName="text-destructive"
        title="Connection Error"
        description={error}
        descriptionClassName="text-destructive"
      />
    )
  }

  // 5. Loaded.
  return (
    <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6">
      <DashboardHeader {...header} />

      {stats && stats.length > 0 && (
        <div className="grid gap-3 sm:gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
          {stats.map((s) => (
            <StatsCard key={s.label} {...s} />
          ))}
        </div>
      )}

      {filterBar && (
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 sm:gap-3">
          {filterBar}
        </div>
      )}

      {/*
        The demoted error surface. Without it a failed background refresh
        would be silent on every page that has no toast of its own
        (Agents/Tasks/Memories/Users/Groups all pass `error` straight to
        this scaffold and surface it nowhere else), leaving the operator
        reading stale rows with no way to tell. One line, in flow, no
        focus steal, no layout jump for the healthy case.
      */}
      {error && (
        <div
          data-slot="stale-notice"
          role="status"
          aria-live="polite"
          className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
          <span>
            Showing the last loaded data — refresh failed: {error}
          </span>
        </div>
      )}

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        {rows.length === 0 ? (
          <EmptyState {...empty} />
        ) : (
          <ResponsiveDataTable
            columns={columns}
            rows={rows}
            getRowId={getRowId}
            onRowClick={onRowClick}
            renderMobileCard={renderMobileCard}
            rowClassName={rowClassName}
            renderExpanded={renderExpanded}
            tableClassName={tableClassName}
          />
        )}
      </div>

      {children}
    </div>
  )
}
