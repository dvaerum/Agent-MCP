"use client"

import * as React from "react"
import { useEffect, useState } from "react"
import { apiClient, type Task } from "@/lib/api"
import { Label } from "@/components/ui/label"
import { ConfirmActionModal } from "@/components/dashboard/modals/confirm-action-modal"
import { DeleteConfirmModal } from "@/components/dashboard/modals/delete-confirm-modal"

type DeletePreview = Awaited<ReturnType<typeof apiClient.getTaskDeletePreview>>

/** Task id block — the one piece of copy both tiers share. */
function TaskIdBlock({ taskId }: { taskId: string }): React.ReactElement {
  return (
    <div className="space-y-2">
      <Label className="text-sm text-muted-foreground">Task ID</Label>
      <div className="text-xs text-muted-foreground font-mono break-all border border-border rounded p-2 bg-muted/30">
        {taskId}
      </div>
    </div>
  )
}

/**
 * Blast-radius preview for a cascading delete — the tier-2 `details`
 * slot. Modelled on `agents/purge-agent-dialog.tsx`'s preview block.
 */
function SubtreePreviewBlock({
  preview,
  error,
}: {
  preview: DeletePreview | null
  error: string | null
}): React.ReactElement {
  return (
    <div className="space-y-2 text-sm">
      {error && (
        <div className="text-sm text-destructive py-1">
          {error}
          <span className="block text-xs text-muted-foreground">
            The blast radius could not be checked, so this delete is gated
            as if it cascades.
          </span>
        </div>
      )}
      {preview && preview.descendant_count > 0 && (
        <>
          <div className="font-medium text-destructive">
            This deletes {preview.descendant_count} sub-task
            {preview.descendant_count === 1 ? "" : "s"} as well:
          </div>
          <ul className="list-disc pl-5 space-y-1 text-muted-foreground max-h-40 overflow-y-auto">
            {preview.descendants.map((d) => (
              <li key={d.task_id} className="break-words">
                {d.title}
                <span className="text-xs block font-mono break-all">
                  {d.task_id} · {d.status}
                  {d.assigned_to ? ` · ${d.assigned_to}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
      {preview && preview.dependent_count > 0 && (
        <div className="text-muted-foreground">
          {preview.dependent_count} other task
          {preview.dependent_count === 1 ? "" : "s"} depend on this one;
          the dependency is dropped and any that become unblocked advance
          to in_progress.
        </div>
      )}
      {preview && preview.blocking_agents.length > 0 && (
        <div className="text-muted-foreground break-words">
          Current task of: {preview.blocking_agents.join(", ")} — the
          pointer is cleared.
        </div>
      )}
      <div className="text-xs text-muted-foreground">
        Deleted tasks are also removed from the RAG index, so this is the
        last searchable copy.
      </div>
    </div>
  )
}

export interface DeleteTaskDialogProps {
  task: Task | null
  onOpenChange: (open: boolean) => void
  onDeleted: () => void
}

/**
 * Task delete — the confirmation model's CONDITIONAL case.
 *
 * The tier is chosen per INVOCATION from the blast radius of this exact
 * click (see `modals/confirm-action-modal.tsx` for the tier table):
 *
 *   * leaf task → **tier 1**, the one-click confirm this page has always
 *     had. Deleting a childless task is a bounded, single-scope action.
 *   * task with a cascade → **tier 2**: the descendant COUNT and the
 *     child titles are shown, and the confirm is gated behind typing
 *     `DELETE`. More *information* is what matters here — the pre-fix
 *     dialog said only "This cannot be undone" while silently taking the
 *     whole subtree, every affected agent's `current_task`, the pruned
 *     `depends_on_tasks` of unrelated tasks, and the RAG index for all of
 *     them.
 *
 * `force` is sent ONLY from the tier-2 branch, so the backend's cascade
 * guard (which returns 409 listing the children) stays armed as a
 * backstop for every other path.
 *
 * Fail-closed: if the preview cannot be fetched we cannot prove the task
 * is a leaf, so the strict gate is used, not the cheap one.
 */
export function DeleteTaskDialog({
  task,
  onOpenChange,
  onDeleted,
}: DeleteTaskDialogProps): React.ReactElement {
  const open = task !== null
  const taskId = task?.task_id ?? ""
  const [preview, setPreview] = useState<DeletePreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !taskId) {
      setPreview(null)
      setPreviewError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setPreview(null)
    setPreviewError(null)
    apiClient
      .getTaskDeletePreview(taskId)
      .then((p) => {
        if (!cancelled) setPreview(p)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setPreviewError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, taskId])

  const runDelete = async (force: boolean) => {
    await apiClient.deleteTask(taskId, { force })
    onDeleted()
  }

  // Escalate on a real cascade OR on an unknown one (preview failed).
  const escalated = previewError !== null || preview?.requires_force === true

  if (escalated) {
    // The count is only claimable when the preview actually landed. On
    // the fail-closed path (`previewError`) we escalated precisely
    // BECAUSE the blast radius is unknown — rendering "Delete 1 tasks"
    // there, as this did before a live pass caught it, is both
    // ungrammatical and a lie about what the button will do.
    const total = preview ? 1 + preview.descendant_count : null
    const confirmLabel =
      total === null
        ? "Delete task and sub-tasks"
        : `Delete ${total} task${total === 1 ? "" : "s"}`
    return (
      <DeleteConfirmModal
        open={open}
        onOpenChange={onOpenChange}
        entityLabel="Task"
        title="Delete task and its sub-tasks"
        description={`Delete “${task?.title ?? ''}” and everything under it? This cannot be undone.`}
        warningTitle="Cascading delete"
        warningText="Deleting a parent task deletes its whole sub-tree. Agents working on any of them lose their current task, tasks that depended on them are unblocked, and all of them are dropped from the searchable RAG index."
        confirmLabel={confirmLabel}
        inputId="delete-task-confirmation"
        details={
          <div className="space-y-3">
            <TaskIdBlock taskId={taskId} />
            <SubtreePreviewBlock preview={preview} error={previewError} />
          </div>
        }
        onConfirm={() => runDelete(true)}
      />
    )
  }

  return (
    <ConfirmActionModal
      open={open}
      onOpenChange={onOpenChange}
      title="Delete task"
      description={`Delete task “${task?.title ?? ''}”? This cannot be undone.`}
      // Until the preview lands we do not know this is a leaf, so the
      // one-click confirm stays disarmed rather than guessing cheap.
      confirmDisabled={loading || preview === null}
      details={
        <div className="space-y-3">
          <TaskIdBlock taskId={taskId} />
          {loading && (
            <div className="text-sm text-muted-foreground">
              Checking for sub-tasks…
            </div>
          )}
        </div>
      }
      onConfirm={() => runDelete(false)}
    />
  )
}
