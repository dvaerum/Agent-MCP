// @vitest-environment jsdom
//
// Unit tests for <ResponsiveDataTable> — the column-spec table that
// renders a <table> on sm+ and a stacked list on mobile from ONE
// Column<T>[]. Retires the "desktop TableRow + separate *-mobile-list"
// double-renderer pattern (architecture review Class 4).
import { describe, it, expect, afterEach, beforeEach, vi } from "vitest"
import * as React from "react"
import {
  render,
  cleanup,
  within,
  fireEvent,
  createEvent,
} from "@testing-library/react"
import { setupUserPlain } from "@/tests/support/user-event"
import { setMatchMedia } from "@/tests/support/match-media"

import {
  ResponsiveDataTable,
  type Column,
} from "@/components/dashboard/shared/responsive-data-table"

// Single-breakpoint render (PF-1): the table renders ONLY the tree for
// the active viewport, chosen via matchMedia. jsdom ships no matchMedia,
// so every test pins one. Default to desktop; mobile tests opt in.
beforeEach(() => setMatchMedia(false))
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
    const u = setupUserPlain()
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
    setMatchMedia(true)
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
    expect(cards[0]!.textContent).toBe("alpha!")
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
    expect(details[0]!.textContent).toBe("detail-alpha")
    const cell = details[0]!.closest("td")!
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
    setMatchMedia(true)
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

  it("applies a per-row rowClassName function to the matching row only", () => {
    // Messages' reply rows carry a left-border indent that depends on
    // the row (`parent_message_id`), which a single static className
    // cannot express — the callback form is the seam for that.
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        rowClassName={(r) => (r.size > 1 ? "border-l-2" : undefined)}
      />,
    )
    const bodyRows = container.querySelectorAll("tbody tr")
    expect(bodyRows[0]!.className).not.toContain("border-l-2")
    expect(bodyRows[1]!.className).toContain("border-l-2")
  })

  it("still accepts a static rowClassName string for every row", () => {
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        rowClassName="ring-1"
      />,
    )
    for (const tr of container.querySelectorAll("tbody tr")) {
      expect(tr.className).toContain("ring-1")
    }
  })

  it("applies rowClassName to the mobile auto-stack item too", () => {
    setMatchMedia(true)
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        rowClassName={(r) => (r.size > 1 ? "border-l-2" : undefined)}
      />,
    )
    const mobile = container.querySelector(
      '[data-slot="data-table-mobile"]',
    ) as HTMLElement
    const items = mobile.querySelectorAll("li")
    expect(items[0]!.className).not.toContain("border-l-2")
    expect(items[1]!.className).toContain("border-l-2")
  })

  it("does NOT leak rowClassName onto the renderExpanded sibling row", () => {
    // The expansion is chrome for the data row and owns its own styling
    // (it opts out of the hover tint); a per-row indent/dim meant for
    // the data row must not silently apply to it.
    const { container } = render(
      <ResponsiveDataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.id}
        rowClassName="border-l-2"
        renderExpanded={(r) =>
          r.id === "a" ? <div data-testid="detail">detail</div> : null
        }
      />,
    )
    const expandedRow = container.querySelector(
      '[data-slot="data-table-expanded"]',
    )!
    expect(expandedRow.className).not.toContain("border-l-2")
    // The data row it belongs to still carries the class.
    expect(container.querySelector("tbody tr")!.className).toContain(
      "border-l-2",
    )
  })

  it("auto-stacks columns on mobile when no renderMobileCard is given", async () => {
    setMatchMedia(true)
    const u = setupUserPlain()
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
    await u.click(items[1]!)
    expect(onRowClick).toHaveBeenCalledWith(rows[1])
  })

  // PF-1 — single-breakpoint render. Only the ACTIVE viewport's tree
  // exists in the DOM; the other is absent, not merely CSS-hidden.
  describe("PF-1 single-breakpoint render", () => {
    it("renders only the desktop table on a desktop viewport", () => {
      setMatchMedia(false)
      const { container } = render(
        <ResponsiveDataTable
          columns={columns}
          rows={rows}
          getRowId={(r) => r.id}
          renderMobileCard={(r) => <li data-testid="mobile-card">{r.name}</li>}
        />,
      )
      expect(container.querySelector("table")).not.toBeNull()
      // The mobile tree must be absent — not present-but-hidden.
      expect(
        container.querySelector('[data-slot="data-table-mobile"]'),
      ).toBeNull()
    })

    it("renders only the mobile list on a mobile viewport", () => {
      setMatchMedia(true)
      const { container } = render(
        <ResponsiveDataTable
          columns={columns}
          rows={rows}
          getRowId={(r) => r.id}
          renderMobileCard={(r) => <li data-testid="mobile-card">{r.name}</li>}
        />,
      )
      expect(
        container.querySelector('[data-slot="data-table-mobile"]'),
      ).not.toBeNull()
      // The desktop tree must be absent — not present-but-hidden.
      expect(container.querySelector("table")).toBeNull()
      expect(
        container.querySelector('[data-slot="data-table-desktop"]'),
      ).toBeNull()
    })

    it("swaps trees when the viewport flips at runtime", () => {
      const mq = setMatchMedia(false)
      const { container } = render(
        <ResponsiveDataTable
          columns={columns}
          rows={rows}
          getRowId={(r) => r.id}
        />,
      )
      expect(container.querySelector("table")).not.toBeNull()
      // Flip to mobile inside act so the effect's listener re-runs.
      React.act(() => mq.flip(true))
      expect(container.querySelector("table")).toBeNull()
      expect(
        container.querySelector('[data-slot="data-table-mobile"]'),
      ).not.toBeNull()
    })
  })

  // AX-1 — keyboard-operable rows. When onRowClick is set the row is a
  // real control: role=button, tabIndex 0, Enter/Space activate it
  // (Space with preventDefault so the page does not scroll).
  describe("AX-1 keyboard-operable rows", () => {
    it("marks the desktop row as a button with a tab stop", () => {
      const { container } = render(
        <ResponsiveDataTable
          columns={columns}
          rows={rows}
          getRowId={(r) => r.id}
          onRowClick={vi.fn()}
        />,
      )
      const row = container.querySelector("tbody tr")!
      expect(row.getAttribute("role")).toBe("button")
      expect(row.getAttribute("tabindex")).toBe("0")
    })

    it("does NOT make the row a button when onRowClick is absent", () => {
      const { container } = render(
        <ResponsiveDataTable
          columns={columns}
          rows={rows}
          getRowId={(r) => r.id}
        />,
      )
      const row = container.querySelector("tbody tr")!
      expect(row.getAttribute("role")).toBeNull()
      expect(row.getAttribute("tabindex")).toBeNull()
    })

    it("fires onRowClick on Enter (desktop row)", () => {
      const onRowClick = vi.fn()
      const { container } = render(
        <ResponsiveDataTable
          columns={columns}
          rows={rows}
          getRowId={(r) => r.id}
          onRowClick={onRowClick}
        />,
      )
      const row = container.querySelector("tbody tr")!
      fireEvent.keyDown(row, { key: "Enter" })
      expect(onRowClick).toHaveBeenCalledWith(rows[0])
    })

    it("fires onRowClick on Space and prevents the default scroll", () => {
      const onRowClick = vi.fn()
      const { container } = render(
        <ResponsiveDataTable
          columns={columns}
          rows={rows}
          getRowId={(r) => r.id}
          onRowClick={onRowClick}
        />,
      )
      const row = container.querySelector("tbody tr")!
      const ev = createEvent.keyDown(row, { key: " " })
      fireEvent(row, ev)
      expect(onRowClick).toHaveBeenCalledWith(rows[0])
      expect(ev.defaultPrevented).toBe(true)
    })

    it("makes the mobile auto-stack item keyboard-operable too", () => {
      setMatchMedia(true)
      const onRowClick = vi.fn()
      const { container } = render(
        <ResponsiveDataTable
          columns={columns}
          rows={rows}
          getRowId={(r) => r.id}
          onRowClick={onRowClick}
        />,
      )
      const item = container.querySelector(
        '[data-slot="data-table-mobile"] li',
      )!
      expect(item.getAttribute("role")).toBe("button")
      expect(item.getAttribute("tabindex")).toBe("0")
      fireEvent.keyDown(item, { key: "Enter" })
      expect(onRowClick).toHaveBeenCalledWith(rows[0])
    })
  })

  // PF-2 — memoized rows. A change to one row (or a background poll that
  // returns a fresh array with unchanged siblings) must not re-run
  // `col.cell` for the rows whose identity did not change.
  describe("PF-2 memoized rows", () => {
    it("does not re-run an unrelated row's cell when another row changes", () => {
      const cellRenders: Record<string, number> = { a: 0, b: 0 }
      const countingColumns: Column<Row>[] = [
        {
          id: "name",
          header: "Name",
          cell: (r) => {
            cellRenders[r.id] = (cellRenders[r.id] ?? 0) + 1
            return r.name
          },
        },
      ]
      function Harness() {
        const [rowsState, setRowsState] = React.useState<Row[]>(rows)
        return (
          <>
            <button
              onClick={() =>
                // New array + new object for `a`; `b` keeps its identity.
                setRowsState((prev) => [
                  { ...prev[0]!, name: "alpha2" },
                  prev[1]!,
                ])
              }
            >
              bump-a
            </button>
            <ResponsiveDataTable
              columns={countingColumns}
              rows={rowsState}
              getRowId={(r) => r.id}
            />
          </>
        )
      }
      const { getByText } = render(<Harness />)
      const bAfterMount = cellRenders.b
      const aAfterMount = cellRenders.a
      fireEvent.click(getByText("bump-a"))
      // b's identity was untouched → its memoized row is skipped.
      expect(cellRenders.b).toBe(bAfterMount)
      // a changed → its cell re-ran.
      expect(cellRenders.a).toBeGreaterThan(aAfterMount)
    })
  })
})
