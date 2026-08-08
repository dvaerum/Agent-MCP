// @vitest-environment jsdom
//
// Unit tests for <DataTablePage> — the list-page scaffold that owns the
// header, stats strip, filter-bar slot, loading skeleton, empty state,
// error panel, forbidden ("Sysadmin only") panel, and the responsive
// table shell (architecture review: the keystone extraction that kills
// Classes 2/3/4 and folds Class 1's list-error case). Pages supply a
// data source + a column spec; the scaffold renders every state.
import { describe, it, expect, afterEach } from "vitest"
import { render, cleanup, screen, within } from "@testing-library/react"
import { Brain, Database, Network } from "lucide-react"

import {
  DataTablePage,
  type DataTablePageProps,
} from "@/components/dashboard/shared/data-table-page"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"

afterEach(() => cleanup())

interface Row {
  id: string
  name: string
}

const columns: Column<Row>[] = [
  { id: "name", header: "Name", cell: (r) => r.name },
]

const rows: Row[] = [
  { id: "a", name: "alpha" },
  { id: "b", name: "beta" },
]

function base(
  overrides: Partial<DataTablePageProps<Row>> = {},
): DataTablePageProps<Row> {
  return {
    header: { title: "Memory Bank", subtitle: "Manage context" },
    loading: false,
    columns,
    rows,
    getRowId: (r) => r.id,
    empty: { icon: Brain, title: "No memories found" },
    ...overrides,
  }
}

describe("<DataTablePage>", () => {
  it("renders header + stats + table body in the loaded, non-empty state", () => {
    render(
      <DataTablePage
        {...base({
          stats: [{ icon: Database, label: "Total", value: 2 }],
          filterBar: <input aria-label="search" />,
        })}
      />,
    )
    expect(screen.getByRole("heading", { name: "Memory Bank" })).toBeTruthy()
    expect(screen.getByText("Total")).toBeTruthy()
    expect(screen.getByLabelText("search")).toBeTruthy()
    const table = document.querySelector("table")!
    expect(within(table).getByText("alpha")).toBeTruthy()
  })

  // The slot was a NON-wrapping `sm:flex-row`. Every migrated page that
  // passes more than a couple of controls (Messages: 7) had to bring its
  // own `sm:flex-wrap` wrapper, and a page that forgot would push its
  // controls off the right edge. Wrapping belongs to the slot: it is a
  // no-op for the 1–3-control pages (they already fit on one line).
  it("wraps the filter-bar slot instead of overflowing it", () => {
    render(<DataTablePage {...base({ filterBar: <input aria-label="search" /> })} />)
    const slot = screen.getByLabelText("search").parentElement!
    expect(slot.className).toContain("sm:flex-row")
    expect(slot.className).toContain("sm:flex-wrap")
  })

  it("renders the empty state (not a table) when rows is empty", () => {
    render(<DataTablePage {...base({ rows: [] })} />)
    expect(screen.getByText("No memories found")).toBeTruthy()
    expect(document.querySelector("table")).toBeNull()
  })

  it("renders the skeleton (not the header) while loading with no rows", () => {
    render(<DataTablePage {...base({ loading: true, rows: [] })} />)
    expect(screen.queryByRole("heading", { name: "Memory Bank" })).toBeNull()
    expect(document.querySelector('[data-slot="table-skeleton"]')).toBeTruthy()
  })

  it("keeps showing content during a background refresh (loading with rows)", () => {
    render(<DataTablePage {...base({ loading: true, rows })} />)
    expect(screen.getByRole("heading", { name: "Memory Bank" })).toBeTruthy()
    expect(document.querySelector('[data-slot="table-skeleton"]')).toBeNull()
  })

  it("renders the error panel (not the header) when error is set with no rows", () => {
    render(<DataTablePage {...base({ error: "boom", rows: [] })} />)
    expect(screen.queryByRole("heading", { name: "Memory Bank" })).toBeNull()
    expect(screen.getByText("boom")).toBeTruthy()
    expect(screen.getByText(/Connection Error/i)).toBeTruthy()
  })

  // The regression this scaffold fix exists for: a polling page
  // (Messages every 60 s + every SSE tick, Tasks every 60 s) hitting one
  // transient failure must NOT blank the page the operator is reading.
  // Error precedence mirrors the loading precedence — the panel owns the
  // page only when there is genuinely nothing else to show.
  it("keeps rendering rows when a refresh fails with content already on screen", () => {
    render(<DataTablePage {...base({ error: "boom", rows })} />)
    expect(screen.getByRole("heading", { name: "Memory Bank" })).toBeTruthy()
    expect(screen.queryByText(/Connection Error/i)).toBeNull()
    const table = document.querySelector("table")!
    expect(within(table).getByText("alpha")).toBeTruthy()
    expect(within(table).getByText("beta")).toBeTruthy()
  })

  it("flags the kept content as stale via a non-blocking notice", () => {
    render(<DataTablePage {...base({ error: "boom", rows })} />)
    const notice = document.querySelector('[data-slot="stale-notice"]')!
    expect(notice).toBeTruthy()
    expect(notice.getAttribute("role")).toBe("status")
    expect(notice.textContent).toContain("boom")
  })

  it("renders no stale notice while healthy", () => {
    render(<DataTablePage {...base()} />)
    expect(document.querySelector('[data-slot="stale-notice"]')).toBeNull()
  })

  it("renders the forbidden panel when forbidden is set", () => {
    render(<DataTablePage {...base({ forbidden: true })} />)
    expect(screen.queryByRole("heading", { name: "Memory Bank" })).toBeNull()
    expect(screen.getByText(/Sysadmin only/i)).toBeTruthy()
  })

  // A 403 is a standing authorization verdict, not a transient blip —
  // it must still replace the page even when stale rows are in hand.
  it("prefers the forbidden panel over the error state, rows or not", () => {
    render(<DataTablePage {...base({ forbidden: true, error: "boom", rows })} />)
    expect(screen.getByText(/Sysadmin only/i)).toBeTruthy()
    expect(document.querySelector("table")).toBeNull()
    expect(screen.queryByText(/Connection Error/i)).toBeNull()
  })

  it("renders the guard panel (short-circuit) when guard is set", () => {
    render(
      <DataTablePage
        {...base({
          guard: {
            icon: Network,
            title: "No Server Connection",
            description: "Connect to an MCP server",
          },
        })}
      />,
    )
    expect(screen.queryByRole("heading", { name: "Memory Bank" })).toBeNull()
    expect(screen.getByText("No Server Connection")).toBeTruthy()
  })

  it("renders children (modals) below the table", () => {
    render(
      <DataTablePage {...base()}>
        <div data-testid="modals-slot" />
      </DataTablePage>,
    )
    expect(screen.getByTestId("modals-slot")).toBeTruthy()
  })
})
