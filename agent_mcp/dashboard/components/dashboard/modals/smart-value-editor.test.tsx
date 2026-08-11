// @vitest-environment jsdom
//
// TY-5 — <SmartValueEditor> array rows must carry a STABLE key, not the
// array index. With index keys, removing a row makes React re-map every
// surviving row onto a different DOM node (the classic index-key
// reconciliation bug): the input that was displaying "banana" ends up
// reused for a different value, so focus/selection/uncontrolled state
// silently jump to the wrong row.
//
// The behavioural pin: capture the DOM node showing a given value,
// remove an EARLIER row, and assert the same value is still rendered by
// the SAME node afterwards. RED with key={index}, GREEN with a stable
// per-row id.
import { describe, it, expect, afterEach } from "vitest"
import { render, cleanup, screen, within } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

import { SmartValueEditor } from "@/components/dashboard/modals/smart-value-editor"

afterEach(() => cleanup())

describe("SmartValueEditor array rows keep stable identity across removal", () => {
  it("preserves the DOM node for a surviving row when an earlier row is removed", async () => {
    const u = setupUser()
    render(<SmartValueEditor value={["apple", "banana", "cherry"]} onChange={() => {}} />)

    // Sanity: three rows, one per value.
    const bananaBefore = screen.getByDisplayValue("banana") as HTMLInputElement
    const appleBefore = screen.getByDisplayValue("apple") as HTMLInputElement
    expect(bananaBefore).toBeTruthy()

    // Remove the FIRST row (apple). Its Trash button is the delete
    // control inside apple's row.
    const appleRow = appleBefore.closest("div") as HTMLElement
    const removeBtn = within(appleRow).getByRole("button")
    await u.click(removeBtn)

    // Banana survives — and must still be rendered by the very same
    // input node (stable key), not a recycled one.
    const bananaAfter = screen.getByDisplayValue("banana")
    expect(bananaAfter).toBe(bananaBefore)
  })
})
