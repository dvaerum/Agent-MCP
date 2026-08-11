// @vitest-environment jsdom
//
// Unit tests for the Messages column spec (Wave 5 extraction — mirrors
// agents/agent-columns.test.tsx). The pre-scaffold `<MessageRow>` encoded
// the table's per-row surface (checkbox + select-all, the unread signal,
// the subject placeholder / reply branch, the delete affordance, and the
// stopPropagation contract that keeps the checkbox/delete from opening the
// detail modal). `useMessagesColumns` is now a seam, so those get pinned
// here rather than by driving the whole page.
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest"
import { render, cleanup, screen, within, fireEvent } from "@testing-library/react"
import { setMatchMedia } from "@/tests/support/match-media"

import {
  useMessagesColumns,
  type MessagesColumnHandlers,
} from "@/components/dashboard/messages/use-messages-columns"
import { ResponsiveDataTable } from "@/components/dashboard/shared/responsive-data-table"
import type { Message } from "@/lib/api"

// PF-1 (Wave 3): the shared table renders only the ACTIVE breakpoint's
// tree, chosen via matchMedia (jsdom ships none). Default to desktop.
beforeEach(() => setMatchMedia(false))
afterEach(() => cleanup())

const mk = (over: Partial<Message>): Message =>
  ({
    message_id: "m1",
    sender_id: "backend-dev",
    recipient_id: "manager",
    message_content: "hello world",
    message_type: "text",
    priority: "high",
    timestamp: "2026-01-01T00:00:00Z",
    delivered: 1,
    read: 0,
    subject: "a subject",
    parent_message_id: null,
    ...over,
  }) as unknown as Message

function Harness({
  rows,
  handlers,
  onRowClick,
}: {
  rows: Message[]
  handlers: MessagesColumnHandlers
  onRowClick?: (m: Message) => void
}) {
  const columns = useMessagesColumns(handlers)
  return (
    <ResponsiveDataTable
      columns={columns}
      rows={rows}
      getRowId={(m) => m.message_id}
      onRowClick={onRowClick}
    />
  )
}

const baseHandlers = (
  over: Partial<MessagesColumnHandlers> = {},
): MessagesColumnHandlers => ({
  selectedIds: new Set<string>(),
  allVisibleSelected: false,
  onToggleAll: () => {},
  onToggleOne: () => {},
  onDelete: () => {},
  labelForParent: (id) => id ?? "",
  ...over,
})

function desktopTable(): HTMLElement {
  return document.querySelector("table")!
}

describe("useMessagesColumns", () => {
  it("renders every column header from the spec", () => {
    render(<Harness rows={[mk({})]} handlers={baseHandlers()} />)
    const table = desktopTable()
    for (const header of [
      "Time",
      "From",
      "To",
      "Subject",
      "Type",
      "Priority",
      "Read?",
      "Content",
    ]) {
      expect(within(table).getByText(header), header).toBeTruthy()
    }
    // The select column's header is the select-all checkbox.
    expect(screen.getByLabelText("select all visible")).toBeTruthy()
  })

  it("wires the select-all header + per-row checkboxes to the handlers", () => {
    const onToggleAll = vi.fn()
    const onToggleOne = vi.fn()
    render(
      <Harness
        rows={[mk({ message_id: "m1" })]}
        handlers={baseHandlers({
          onToggleAll,
          onToggleOne,
          selectedIds: new Set(["m1"]),
        })}
      />,
    )
    fireEvent.click(screen.getByLabelText("select all visible"))
    expect(onToggleAll).toHaveBeenCalledTimes(1)

    const rowCheckbox = screen.getByLabelText("select message m1") as HTMLInputElement
    // selectedIds carries m1, so it renders checked.
    expect(rowCheckbox.checked).toBe(true)
    fireEvent.click(rowCheckbox)
    expect(onToggleOne).toHaveBeenCalledWith("m1")
  })

  it("routes the delete cell through onDelete without firing the row click", () => {
    const onDelete = vi.fn()
    const onRowClick = vi.fn()
    render(
      <Harness
        rows={[mk({ message_id: "m1" })]}
        handlers={baseHandlers({ onDelete })}
        onRowClick={onRowClick}
      />,
    )
    const table = desktopTable()
    // Clicking the delete button fires onDelete but NOT the row-body
    // onRowClick (the cell stopPropagation's the synthetic event).
    fireEvent.click(within(table).getByLabelText("delete message"))
    expect(onDelete).toHaveBeenCalledWith("m1")
    expect(onRowClick).not.toHaveBeenCalled()

    // Sanity: clicking the row body DOES open detail.
    fireEvent.click(within(table).getByText("a subject"))
    expect(onRowClick).toHaveBeenCalledTimes(1)
  })

  it("shows an unread signal for unread rows and none for read rows", () => {
    const { rerender } = render(
      <Harness rows={[mk({ message_id: "m1", read: 0 })]} handlers={baseHandlers()} />,
    )
    // Unread: the sr-only Read? cell announces "unread".
    expect(within(desktopTable()).getByText("unread")).toBeTruthy()

    rerender(
      <Harness rows={[mk({ message_id: "m1", read: 1 })]} handlers={baseHandlers()} />,
    )
    expect(within(desktopTable()).getByText("read")).toBeTruthy()
  })

  it("renders the reply parent label for a reply with no subject", () => {
    const labelForParent = vi.fn(() => "the parent subject")
    render(
      <Harness
        rows={[mk({ message_id: "m2", subject: null, parent_message_id: "m1" })]}
        handlers={baseHandlers({ labelForParent })}
      />,
    )
    expect(labelForParent).toHaveBeenCalledWith("m1")
    expect(within(desktopTable()).getByText("the parent subject")).toBeTruthy()
  })
})
