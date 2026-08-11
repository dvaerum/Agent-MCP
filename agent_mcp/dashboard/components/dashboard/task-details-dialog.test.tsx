// @vitest-environment jsdom
//
// AX-5 — every Radix DialogContent needs a DialogDescription (else Radix
// warns and the dialog exposes no accessible description). TaskDetailsDialog
// was missing one; this pins that it now exposes an accessible description.
import { describe, it, expect, afterEach } from "vitest"
import { render, cleanup, screen } from "@testing-library/react"

import { TaskDetailsDialog } from "@/components/dashboard/task-details-dialog"
import type { Task } from "@/lib/api"

const task: Task = {
  task_id: "t-1",
  title: "Ship the thing",
  status: "in_progress",
  priority: "high",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
}

afterEach(() => cleanup())

/** Resolve an element's accessible description from aria-describedby. */
function describedText(node: HTMLElement): string {
  const ids = (node.getAttribute("aria-describedby") ?? "").split(/\s+/)
  return ids
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ")
}

describe("<TaskDetailsDialog> accessibility (AX-5)", () => {
  it("exposes an accessible description", () => {
    render(<TaskDetailsDialog task={task} open onOpenChange={() => {}} />)
    const dialog = screen.getByRole("dialog")
    expect(describedText(dialog).trim().length).toBeGreaterThan(0)
  })
})
