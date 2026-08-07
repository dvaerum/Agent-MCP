// @vitest-environment jsdom
//
// Unit tests for <ResponsiveDataTable> — the column-spec table that
// renders a <table> on sm+ and a stacked list on mobile from ONE
// Column<T>[]. Retires the "desktop TableRow + separate *-mobile-list"
// double-renderer pattern (architecture review Class 4).
import { describe, it, expect, afterEach, vi } from "vitest"
import { render, cleanup, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import {
  ResponsiveDataTable,
  type Column,
} from "@/components/dashboard/shared/responsive-data-table"

afterEach(() => cleanup())

interface Row {
  id: string
  name: string
  size: number
}

const rows: Row[] = [
  { id: "a", name: "alpha", size: 1 },
  { id: "b", name: "beta", size: 2 },
]

const columns: Column<Row>[] = [
  { id: "name", header: "Name", cell: (r) => r.name },
  { id: "size", header: "Size", cell: (r) => r.size, hideBelow: "md" },
]

describe("<ResponsiveDataTable>", () => {
  it("renders column headers and cells in the desktop table", () => {
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
      />,
    )
    const table = container.querySelector("table")!
    const scope = within(table)
    expect(scope.getByText("Name")).toBeTruthy()
    expect(scope.getByText("Size")).toBeTruthy()
    expect(scope.getByText("alpha")).toBeTruthy()
    expect(scope.getByText("beta")).toBeTruthy()
  })

  it("fires onRowClick with the row when a desktop row is clicked", async () => {
    const u = userEvent.setup()
    const onRowClick = vi.fn()
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        onRowClick={onRowClick}
      />,
    )
    const bodyRow = container.querySelector("tbody tr")!
    await u.click(bodyRow)
    expect(onRowClick).toHaveBeenCalledWith(rows[0])
  })

  it("applies hideBelow responsive classes to both head and body cells", () => {
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
      />,
    )
    const table = container.querySelector("table")!
    const sizeHead = within(table).getByText("Size").closest("th")!
    expect(sizeHead.className).toContain("md:table-cell")
  })

  it("uses renderMobileCard for the mobile section when provided", () => {
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        renderMobileCard={(r) => <li data-testid="mobile-card">{r.name}!</li>}
      />,
    )
    const mobile = container.querySelector('[data-slot="data-table-mobile"]')!
    const cards = within(mobile as HTMLElement).getAllByTestId("mobile-card")
    expect(cards).toHaveLength(2)
    expect(cards[0].textContent).toBe("alpha!")
  })

  // renderExpanded — the accordion seam (Groups: a row expands to its
  // members + capabilities). Desktop gets a full-width colSpan sibling
  // row; the mobile auto-stack appends the content inside the <li>.
  it("emits a full-width colSpan row beneath an expanded desktop row", () => {
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        renderExpanded={(r) =>
          r.id === "a" ? <div data-testid="detail">detail-{r.name}</div> : null
        }
      />,
    )
    const table = container.querySelector("table")!
    const details = within(table).getAllByTestId("detail")
    expect(details).toHaveLength(1)
    expect(details[0].textContent).toBe("detail-alpha")
    const cell = details[0].closest("td")!
    expect(cell.getAttribute("colspan")).toBe(String(columns.length))
  })

  it("renders no expansion row when renderExpanded returns falsy", () => {
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        renderExpanded={() => null}
      />,
    )
    expect(
      container.querySelectorAll('[data-slot="data-table-expanded"]'),
    ).toHaveLength(0)
    // …and the plain body rows are untouched.
    const table = container.querySelector("table")!
    expect(table.querySelectorAll("tbody tr")).toHaveLength(2)
  })

  it("appends expanded content inside the mobile auto-stack item", () => {
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        renderExpanded={(r) =>
          r.id === "b" ? <div data-testid="detail">{r.name}-detail</div> : null
        }
      />,
    )
    const mobile = container.querySelector(
      '[data-slot="data-table-mobile"]',
    ) as HTMLElement
    const items = mobile.querySelectorAll("li")
    expect(items).toHaveLength(2)
    expect(within(items[0] as HTMLElement).queryByTestId("detail")).toBeNull()
    expect(
      within(items[1] as HTMLElement).getByTestId("detail").textContent,
    ).toBe("beta-detail")
  })

  it("auto-stacks columns on mobile when no renderMobileCard is given", async () => {
    const u = userEvent.setup()
    const onRowClick = vi.fn()
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        onRowClick={onRowClick}
      />,
    )
    const mobile = container.querySelector(
      '[data-slot="data-table-mobile"]',
    ) as HTMLElement
    // auto-stack renders each row; clicking one fires onRowClick
    const items = mobile.querySelectorAll("li")
    expect(items).toHaveLength(2)
    await u.click(items[1])
    expect(onRowClick).toHaveBeenCalledWith(rows[1])
  })
})
