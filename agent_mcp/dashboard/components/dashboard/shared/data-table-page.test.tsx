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

  it("renders the error panel (not the header) when error is set", () => {
    render(<DataTablePage {...base({ error: "boom" })} />)
    expect(screen.queryByRole("heading", { name: "Memory Bank" })).toBeNull()
    expect(screen.getByText("boom")).toBeTruthy()
    expect(screen.getByText(/Connection Error/i)).toBeTruthy()
  })

  it("renders the forbidden panel when forbidden is set", () => {
    render(<DataTablePage {...base({ forbidden: true })} />)
    expect(screen.queryByRole("heading", { name: "Memory Bank" })).toBeNull()
    expect(screen.getByText(/Sysadmin only/i)).toBeTruthy()
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
