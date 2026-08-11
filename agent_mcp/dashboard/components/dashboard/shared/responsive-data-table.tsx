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
import { useMediaQuery } from "@/hooks/use-media-query"

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
   * `hidden <bp>:table-cell` to head + body). The desktop table only
   * renders on the desktop viewport now (PF-1), so `sm` is effectively
   * "always shown in the desktop table" — kept for faithful
   * column-parity.
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

// PF-1 — the table renders only ONE of its two trees. The split used to
// be pure CSS (`hidden sm:block` desktop / `block sm:hidden` mobile) at
// Tailwind's `sm` (640px). This query is the JS mirror of that exact
// boundary so the runtime swap lands on the same pixel the old CSS did
// — do NOT reuse `useIsMobile` (768px), which would shift the split.
const MOBILE_QUERY = "(max-width: 639px)"

// Shared focus ring for the role=button rows (AX-1). Keyboard users get
// a visible target; mouse users don't (`:focus-visible`, not `:focus`).
const FOCUS_RING =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"

/**
 * AX-1 — activate a role=button row from the keyboard. Enter and Space
 * both fire; Space additionally `preventDefault`s so the page doesn't
 * scroll (a `<tr>`/`<li>` has no native activation, so without this the
 * row would be a focus trap that only the mouse could operate).
 */
function makeRowKeyDown(
  onActivate: () => void,
): (e: React.KeyboardEvent) => void {
  return (e) => {
    if (e.key === "Enter") {
      onActivate()
    } else if (e.key === " " || e.key === "Spacebar") {
      e.preventDefault()
      onActivate()
    }
  }
}

interface DataRowProps<T> {
  row: T
  columns: Column<T>[]
  /**
   * Stable click handler (the parent ref-wraps the consumer's
   * onRowClick so this identity survives re-renders — the seam that
   * lets `React.memo` skip untouched rows). Undefined ⇒ non-clickable.
   */
  onActivate?: (row: T) => void
  rowClassName?: ResponsiveDataTableProps<T>["rowClassName"]
  renderExpanded?: (row: T) => React.ReactNode
}

/**
 * PF-2 — a single desktop body row (plus its optional expansion
 * sibling), memoized so a re-render of the table that leaves this row's
 * identity untouched does not re-run its `col.cell`.
 */
function DesktopRowInner<T>({
  row,
  columns,
  onActivate,
  rowClassName,
  renderExpanded,
}: DataRowProps<T>): React.ReactElement {
  const expanded = renderExpanded?.(row)
  const clickable = !!onActivate
  const fire = () => onActivate?.(row)
  return (
    <>
      <TableRow
        className={cn(
          "border-border/50 hover:bg-muted/30 group transition-all duration-200",
          clickable && `cursor-pointer ${FOCUS_RING}`,
          resolveRowClassName(rowClassName, row),
        )}
        onClick={clickable ? fire : undefined}
        role={clickable ? "button" : undefined}
        tabIndex={clickable ? 0 : undefined}
        onKeyDown={clickable ? makeRowKeyDown(fire) : undefined}
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
    </>
  )
}
// React.memo drops the generic signature; cast it back so call sites
// keep full `Column<T>` inference (standard generic-memo idiom).
const DesktopRow = React.memo(DesktopRowInner) as typeof DesktopRowInner

/**
 * PF-2 — a single mobile auto-stack item, memoized on the same terms as
 * the desktop row.
 */
function MobileAutoStackItemInner<T>({
  row,
  columns,
  onActivate,
  rowClassName,
  renderExpanded,
}: DataRowProps<T>): React.ReactElement {
  const clickable = !!onActivate
  const fire = () => onActivate?.(row)
  return (
    <li
      onClick={clickable ? fire : undefined}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={clickable ? makeRowKeyDown(fire) : undefined}
      className={cn(
        "px-4 py-3 space-y-1",
        clickable &&
          `hover:bg-muted/30 active:bg-muted/50 transition-colors duration-150 cursor-pointer ${FOCUS_RING}`,
        // The auto-stack <li> IS the mobile data row, so it takes the
        // same per-row treatment as the desktop <tr>. (A page with its
        // own renderMobileCard owns that markup — and its per-row
        // styling — itself.)
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
  )
}
const MobileAutoStackItem = React.memo(
  MobileAutoStackItemInner,
) as typeof MobileAutoStackItemInner

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
  // PF-1 — render ONLY the active breakpoint's tree, not both-and-hide.
  // `useMediaQuery` returns false (desktop) on the first render so the
  // static export's client-only first paint is deterministic; the
  // effect then swaps to mobile if the viewport is narrow.
  const isMobile = useMediaQuery(MOBILE_QUERY)

  // Stable "activate this row" callback (PF-2). Consumers routinely
  // pass an inline `onRowClick={(r) => open(r.id)}` whose identity
  // changes every render; ref-wrapping it here means the memoized rows
  // see ONE handler reference for the table's lifetime, so an unrelated
  // re-render can't invalidate every row through the click prop. Clicks
  // still read the latest handler via the ref.
  const onRowClickRef = React.useRef(onRowClick)
  onRowClickRef.current = onRowClick
  const activate = React.useCallback((row: T) => {
    onRowClickRef.current?.(row)
  }, [])
  const onActivate = onRowClick ? activate : undefined

  if (isMobile) {
    return (
      <div data-slot="data-table-mobile">
        <ul role="list" className="divide-y divide-border">
          {rows.map((row) =>
            renderMobileCard ? (
              <React.Fragment key={getRowId(row)}>
                {renderMobileCard(row)}
              </React.Fragment>
            ) : (
              <MobileAutoStackItem
                key={getRowId(row)}
                row={row}
                columns={columns}
                onActivate={onActivate}
                rowClassName={rowClassName}
                renderExpanded={renderExpanded}
              />
            ),
          )}
        </ul>
      </div>
    )
  }

  return (
    <div data-slot="data-table-desktop" className="overflow-x-auto">
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
          {rows.map((row) => (
            <DesktopRow
              key={getRowId(row)}
              row={row}
              columns={columns}
              onActivate={onActivate}
              rowClassName={rowClassName}
              renderExpanded={renderExpanded}
            />
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
