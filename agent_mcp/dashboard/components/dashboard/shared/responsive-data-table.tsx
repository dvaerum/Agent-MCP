"use client"

import * as React from "react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

/**
 * One column spec, consumed for BOTH the desktop table and the mobile
 * auto-stack. A page that needs a bespoke mobile card (e.g. memories,
 * whose mobile layout genuinely differs from its columns) supplies
 * `renderMobileCard` instead and the mobile auto-stack is skipped.
 */
export interface Column<T> {
  /** Stable id (React key for header/cell + column identity). */
  id: string
  /** Column header content. */
  header: React.ReactNode
  /** Cell renderer for a row. */
  cell: (row: T) => React.ReactNode
  /** Extra classes on the <th>. */
  headClassName?: string
  /** Extra classes on the <td>. */
  cellClassName?: string
  /**
   * Hide the column below this breakpoint in the desktop table (adds
   * `hidden <bp>:table-cell` to head + body). The whole desktop table
   * is already `hidden sm:block`, so `sm` is effectively "always shown
   * in the desktop table" — kept for faithful column-parity.
   */
  hideBelow?: "sm" | "md" | "lg"
  /** Label shown before the value in the mobile auto-stack. */
  mobileLabel?: string
  /** Omit this column from the mobile auto-stack. */
  hideOnMobile?: boolean
}

export interface ResponsiveDataTableProps<T> {
  columns: Column<T>[]
  rows: T[]
  /** Stable identity per row (React key + click payload lookups). */
  getRowId: (row: T) => string
  /** Row click (desktop row + mobile item). */
  onRowClick?: (row: T) => void
  /**
   * Custom mobile renderer. Return the full list item (`<li>`); the
   * table wraps them in a `<ul role="list" class="divide-y">`. When
   * omitted, the mobile section auto-stacks the columns.
   */
  renderMobileCard?: (row: T) => React.ReactNode
  /**
   * Extra classes on the DATA row — the desktop body `<tr>` and the
   * mobile auto-stack `<li>`. Pass a callback for a per-row treatment a
   * single static string cannot express (messages' reply rows carry a
   * left-border indent keyed on `parent_message_id`).
   *
   * Deliberately NOT applied to the `renderExpanded` sibling row: that
   * row is chrome for the data row, owns its own styling (it opts out
   * of the hover tint), and a per-row class such as a left-border
   * indent or an opacity dim is meant for the data row alone. A page
   * that wants its expansion styled per-row can do it inside its own
   * `renderExpanded` markup, where it has the row in hand.
   */
  rowClassName?: string | ((row: T) => string | undefined)
  /**
   * Optional expandable detail for a row (accordion pages such as
   * Groups, whose row expands to show its members + capabilities).
   * Return a falsy value for a collapsed row.
   *
   * Desktop: a full-width `colSpan` sibling `<tr>` is emitted directly
   * beneath the row. Mobile auto-stack: the content is appended inside
   * the row's `<li>`. A page supplying `renderMobileCard` owns its
   * mobile rendering entirely and therefore its own mobile expansion.
   */
  renderExpanded?: (row: T) => React.ReactNode
  /**
   * Extra classes on the desktop `<table>` itself.
   *
   * Exists for `table-fixed`: a page whose cells can hold unbounded
   * user-supplied text (e.g. the Agents table's `agent_id`) needs fixed
   * layout so one pathological value truncates within its column
   * instead of stretching the auto-layout table thousands of px wide
   * and pushing every other column off-screen. Column widths then come
   * from each column's `headClassName`.
   */
  tableClassName?: string
}

/** Resolve the static-or-callback `rowClassName` for one row. */
function resolveRowClassName<T>(
  rowClassName: ResponsiveDataTableProps<T>["rowClassName"],
  row: T,
): string | undefined {
  return typeof rowClassName === "function" ? rowClassName(row) : rowClassName
}

const HIDE_BELOW_CLASS: Record<NonNullable<Column<unknown>["hideBelow"]>, string> = {
  sm: "hidden sm:table-cell",
  md: "hidden md:table-cell",
  lg: "hidden lg:table-cell",
}

export function ResponsiveDataTable<T>({
  columns,
  rows,
  getRowId,
  onRowClick,
  renderMobileCard,
  rowClassName,
  renderExpanded,
  tableClassName,
}: ResponsiveDataTableProps<T>): React.ReactElement {
  return (
    <>
      {/* Desktop table (sm+) */}
      <div
        data-slot="data-table-desktop"
        className="hidden sm:block overflow-x-auto"
      >
        <Table className={tableClassName}>
          <TableHeader>
            <TableRow className="border-border hover:bg-transparent">
              {columns.map((col) => (
                <TableHead
                  key={col.id}
                  className={cn(
                    "text-muted-foreground font-medium text-xs uppercase tracking-wider",
                    col.hideBelow && HIDE_BELOW_CLASS[col.hideBelow],
                    col.headClassName,
                  )}
                >
                  {col.header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => {
              const expanded = renderExpanded?.(row)
              return (
                <React.Fragment key={getRowId(row)}>
                  <TableRow
                    className={cn(
                      "border-border/50 hover:bg-muted/30 group transition-all duration-200",
                      onRowClick && "cursor-pointer",
                      resolveRowClassName(rowClassName, row),
                    )}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                  >
                    {columns.map((col) => (
                      <TableCell
                        key={col.id}
                        className={cn(
                          col.hideBelow && HIDE_BELOW_CLASS[col.hideBelow],
                          col.cellClassName,
                        )}
                      >
                        {col.cell(row)}
                      </TableCell>
                    ))}
                  </TableRow>
                  {expanded ? (
                    <TableRow
                      data-slot="data-table-expanded"
                      className="border-border/50 hover:bg-transparent"
                    >
                      <TableCell colSpan={columns.length} className="p-0">
                        {expanded}
                      </TableCell>
                    </TableRow>
                  ) : null}
                </React.Fragment>
              )
            })}
          </TableBody>
        </Table>
      </div>

      {/* Mobile (below sm) */}
      <div data-slot="data-table-mobile" className="block sm:hidden">
        <ul role="list" className="divide-y divide-border">
          {rows.map((row) =>
            renderMobileCard ? (
              <React.Fragment key={getRowId(row)}>
                {renderMobileCard(row)}
              </React.Fragment>
            ) : (
              <li
                key={getRowId(row)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={cn(
                  "px-4 py-3 space-y-1",
                  onRowClick &&
                    "hover:bg-muted/30 active:bg-muted/50 transition-colors duration-150 cursor-pointer",
                  // The auto-stack <li> IS the mobile data row, so it
                  // takes the same per-row treatment as the desktop
                  // <tr>. (A page with its own renderMobileCard owns
                  // that markup — and its per-row styling — itself.)
                  resolveRowClassName(rowClassName, row),
                )}
              >
                {columns
                  .filter((col) => !col.hideOnMobile)
                  .map((col) => (
                    <div
                      key={col.id}
                      className="flex items-baseline justify-between gap-3 text-sm"
                    >
                      {col.mobileLabel && (
                        <span className="text-xs text-muted-foreground">
                          {col.mobileLabel}
                        </span>
                      )}
                      <span className="min-w-0">{col.cell(row)}</span>
                    </div>
                  ))}
                {renderExpanded?.(row)}
              </li>
            ),
          )}
        </ul>
      </div>
    </>
  )
}
