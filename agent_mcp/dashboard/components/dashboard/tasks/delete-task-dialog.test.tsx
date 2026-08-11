// @vitest-environment jsdom
//
// Task delete = the confirmation model's CONDITIONAL case: the tier
// follows the blast radius of THIS click, not the entity type.
//
//   * leaf task        -> tier 1, one-click confirm, force_delete=false
//   * task w/ subtree  -> tier 2, count + titles + type DELETE, force=true
//
// RED before the split; the pre-fix dialog was a one-click confirm that
// fired a hardcoded server-side force_delete=true, so a parent's entire
// descendant subtree died behind copy that never named a count.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

const getTaskDeletePreview = vi.fn()
const deleteTask = vi.fn()
vi.mock("@/lib/api", () => ({
  apiClient: {
    getTaskDeletePreview: (...a: unknown[]) => getTaskDeletePreview(...a),
    deleteTask: (...a: unknown[]) => deleteTask(...a),
  },
  ApiError: class ApiError extends Error {},
}))

import { DeleteTaskDialog } from "@/components/dashboard/tasks/delete-task-dialog"
import type { Task } from "@/lib/api"

const task = {
  task_id: "task_abc123",
  title: "Ship the thing",
  description: "",
  status: "pending",
  priority: "medium",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  assigned_to: null,
  created_by: "admin",
} as unknown as Task

const leafPreview = {
  task_id: task.task_id,
  title: task.title,
  descendant_count: 0,
  descendants: [],
  dependent_count: 0,
  dependents: [],
  blocking_agents: [],
  requires_force: false,
}

const subtreePreview = {
  task_id: task.task_id,
  title: task.title,
  descendant_count: 2,
  descendants: [
    { task_id: "task_kid1", title: "Wire the API", status: "pending", assigned_to: "worker-1" },
    { task_id: "task_kid2", title: "Write the docs", status: "in_progress", assigned_to: null },
  ],
  dependent_count: 0,
  dependents: [],
  blocking_agents: [],
  requires_force: true,
}

afterEach(() => {
  cleanup()
  getTaskDeletePreview.mockReset()
  deleteTask.mockReset()
})

function renderDialog(onDeleted = vi.fn(), onOpenChange = vi.fn()) {
  render(
    <DeleteTaskDialog
      task={task}
      onOpenChange={onOpenChange}
      onDeleted={onDeleted}
    />,
  )
  return { onDeleted, onOpenChange }
}

describe("<DeleteTaskDialog> — tier follows the blast radius", () => {
  it("leaf task: one-click confirm, no type-to-confirm field", async () => {
    getTaskDeletePreview.mockResolvedValue(leafPreview)
    deleteTask.mockResolvedValue({ success: true })
    const { onDeleted } = renderDialog()

    await waitFor(() => expect(getTaskDeletePreview).toHaveBeenCalledWith(task.task_id))
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Delete$/ })).toHaveProperty(
        "disabled",
        false,
      ),
    )
    expect(document.querySelector('input[id*="confirm"]')).toBeNull()

    await setupUser().click(screen.getByRole("button", { name: /^Delete$/ }))
    await waitFor(() =>
      expect(deleteTask).toHaveBeenCalledWith(task.task_id, { force: false }),
    )
    expect(onDeleted).toHaveBeenCalled()
  })

  it("leaf task: still names the target and says it cannot be undone", async () => {
    getTaskDeletePreview.mockResolvedValue(leafPreview)
    renderDialog()
    await waitFor(() => expect(screen.getByText(/Ship the thing/)).toBeTruthy())
    expect(screen.getByText(/cannot be undone/i)).toBeTruthy()
  })

  it("subtree: names the COUNT and the child titles", async () => {
    getTaskDeletePreview.mockResolvedValue(subtreePreview)
    renderDialog()
    await waitFor(() => expect(screen.getByText(/2 sub-?tasks?/i)).toBeTruthy())
    expect(screen.getByText(/Wire the API/)).toBeTruthy()
    expect(screen.getByText(/Write the docs/)).toBeTruthy()
  })

  it("subtree: escalates to type-DELETE and only then force-deletes", async () => {
    getTaskDeletePreview.mockResolvedValue(subtreePreview)
    deleteTask.mockResolvedValue({ success: true })
    const { onDeleted } = renderDialog()

    const confirm = await screen.findByRole("button", { name: /Delete 3 tasks/i })
    expect(confirm).toHaveProperty("disabled", true)

    await setupUser().type(
      screen.getByLabelText(/to confirm deletion/i),
      "DELETE",
    )
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false))
    await setupUser().click(confirm)

    await waitFor(() =>
      expect(deleteTask).toHaveBeenCalledWith(task.task_id, { force: true }),
    )
    expect(onDeleted).toHaveBeenCalled()
  })

  it("never sends force=true from the tier-1 branch", async () => {
    getTaskDeletePreview.mockResolvedValue(leafPreview)
    deleteTask.mockResolvedValue({ success: true })
    renderDialog()
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Delete$/ })).toHaveProperty(
        "disabled",
        false,
      ),
    )
    await setupUser().click(screen.getByRole("button", { name: /^Delete$/ }))
    await waitFor(() => expect(deleteTask).toHaveBeenCalled())
    expect(deleteTask.mock.calls[0]![1]).toEqual({ force: false })
  })

  it("a task blocked only by dependents says 'Delete 1 task', not '1 tasks'", async () => {
    getTaskDeletePreview.mockResolvedValue({
      ...leafPreview,
      dependent_count: 2,
      dependents: [
        { task_id: "task_dep1", title: "Downstream A" },
        { task_id: "task_dep2", title: "Downstream B" },
      ],
      requires_force: true,
    })
    renderDialog()
    expect(await screen.findByRole("button", { name: /Delete 1 task$/ })).toBeTruthy()
  })

  it("an UNKNOWN blast radius must not claim a count", async () => {
    // Fail-closed still escalates, but the dialog has no idea how many
    // tasks die — asserting "Delete 1 tasks" (what it used to render)
    // is both ungrammatical and a lie.
    getTaskDeletePreview.mockRejectedValue(new Error("504: backend failed"))
    renderDialog()
    await waitFor(() => expect(screen.getByText(/504: backend failed/)).toBeTruthy())
    expect(screen.queryByRole("button", { name: /Delete 1 tasks?$/ })).toBeNull()
    expect(
      screen.getByRole("button", { name: /Delete task and sub-tasks/ }),
    ).toBeTruthy()
  })

  it("a failed preview does NOT fall back to the cheap tier", async () => {
    // Fail closed: if we cannot prove the task is a leaf, the operator
    // gets the strict gate, not the one-click confirm.
    getTaskDeletePreview.mockRejectedValue(new Error("preview exploded"))
    renderDialog()
    await waitFor(() => expect(screen.getByText(/preview exploded/)).toBeTruthy())
    expect(screen.getByLabelText(/to confirm deletion/i)).toBeTruthy()
  })

  it("surfaces a failed delete inline and keeps the dialog open", async () => {
    getTaskDeletePreview.mockResolvedValue(leafPreview)
    deleteTask.mockRejectedValue(new Error("db locked"))
    const { onDeleted, onOpenChange } = renderDialog()
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Delete$/ })).toHaveProperty(
        "disabled",
        false,
      ),
    )
    await setupUser().click(screen.getByRole("button", { name: /^Delete$/ }))
    await waitFor(() => expect(screen.getByText("db locked")).toBeTruthy())
    expect(onDeleted).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })
})
