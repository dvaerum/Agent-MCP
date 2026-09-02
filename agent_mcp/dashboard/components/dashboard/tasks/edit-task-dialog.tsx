"use client"

import { useEffect, useState } from "react"
import { Pencil } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Label } from "@/components/ui/label"
import { apiClient, type Task } from "@/lib/api"
import { cn, formatRelative } from "@/lib/utils"
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import { isTombstone, parseTaskComments } from "@/components/dashboard/tasks/tasks-api"

export interface EditTaskDialogProps {
  task: Task | null
  onOpenChange: (open: boolean) => void
  onSaved: () => void
}

/**
 * Edit task — Wave 5: adopts the shared `<FormDialog>` + `useAsyncSubmit`
 * shell (mirrors messages' compose + the agents forms). The shell owns
 * the Dialog chrome, the Cancel/Save footer, the in-flight spinner and
 * the success/error toast + close-on-success semantics; `onSubmit` just
 * builds the patch, mutates, refreshes and THROWS on failure so the shell
 * keeps the dialog open with the operator's edits intact.
 */
export function EditTaskDialog({ task, onOpenChange, onSaved }: EditTaskDialogProps) {
  const open = task !== null

  const [editTitle, setEditTitle] = useState('')
  const [editDescription, setEditDescription] = useState('')
  const [editStatus, setEditStatus] = useState<Task['status'] | 'unassigned'>('pending')
  const [editPriority, setEditPriority] = useState<Task['priority']>('medium')
  // AgentSelect speaks `string | null` directly; null = unassigned.
  const [editAssignedTo, setEditAssignedTo] = useState<string | null>(null)
  // New-comment textarea is append-only: the backend stores notes as a
  // JSON array and `/api/update-task-dashboard` appends a single
  // entry per request. Empty string = no comment added. Cleared on save.
  const [editComment, setEditComment] = useState<string>('')

  // Existing comments for the "Existing comments" preview block at the
  // bottom of the Edit dialog. Read-only here — to edit historical
  // comments you'd need per-comment IDs which don't exist in the schema.
  const existingComments = task ? parseTaskComments(task.notes) : []

  // Re-seed form whenever the dialog opens for a *different* task.
  // Note: with live-lookup useDialog (Candidate D, 2026-06-02) the
  // `task` prop reference can change on every background refresh
  // even when the underlying fields are unchanged — keying the effect
  // on task identity prevents the refresh from blowing away the
  // admin's in-progress edits. Only the New-comment textarea is reset
  // between opens; existing field edits survive.
  const taskId = task?.task_id
  useEffect(() => {
    if (!task) return
    setEditTitle(task.title || '')
    setEditDescription(task.description || '')
    setEditStatus(task.status || 'pending')
    setEditPriority(task.priority || 'medium')
    setEditAssignedTo(task.assigned_to || null)
    setEditComment('')
    // We deliberately depend on taskId, not the whole task object —
    // see the comment above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId])

  // Pre-PR: this dialog fetched its own agent list via the unfiltered
  // apiClient.getAgents() endpoint, which returns every row including
  // status='terminated' — leaking ghost agents into the Assigned-to
  // dropdown. Replaced 2026-06-04 by the shared <AgentSelect>, which
  // reads from data-store::getActiveAgents() (live-only). No local
  // fetch needed.

  // The save mutation. Throws on failure so the shared shell keeps the
  // dialog open with the operator's edits intact + surfaces the toast.
  const handleSave = async () => {
    if (!task) return
    // Build the patch: include every editable field so the admin can
    // blanket-overwrite, but normalise assigned_to so the sentinel
    // becomes a real null.
    const patch: Record<string, unknown> = {
      title: editTitle,
      description: editDescription,
      status: editStatus as Task['status'],
      priority: editPriority,
      // AgentSelect speaks string|null directly — pass it through.
      assigned_to: editAssignedTo,
    }
    // Append-only: only include `notes` in the patch when the new-comment
    // textarea has content. The backend treats `notes: str` as "append
    // a new entry with author=admin + timestamp"; passing empty would
    // be a no-op but we omit it to keep the request body minimal.
    const trimmedComment = editComment.trim()
    if (trimmedComment) patch.notes = trimmedComment
    await apiClient.updateTask(task.task_id, patch)
    onSaved()
    setEditComment('')
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Edit task"
      description="Changes are saved via POST /api/update-task-dashboard."
      icon={Pencil}
      wide
      onSubmit={handleSave}
      submitLabel="Save"
      submittingLabel="Saving…"
      submitDisabled={!editTitle.trim()}
      successMessage="Task updated."
      errorMessage="Failed to save task"
    >
      {task && (
        <>
          <div className="space-y-2">
            <Label htmlFor="edit-task-title" className="text-sm text-muted-foreground">Title</Label>
            <Input
              id="edit-task-title"
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
              required
              className="w-full bg-background border-border text-foreground"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="edit-task-description" className="text-sm text-muted-foreground">Description</Label>
            <Textarea
              id="edit-task-description"
              value={editDescription}
              onChange={(e) => setEditDescription(e.target.value)}
              className="w-full bg-background border-border text-foreground min-h-[100px] whitespace-pre-wrap font-mono text-xs"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="edit-task-status" className="text-sm text-muted-foreground">Status</Label>
              <Select value={editStatus} onValueChange={(v) => setEditStatus(v as Task['status'])}>
                <SelectTrigger id="edit-task-status" className="w-full bg-background border-border text-foreground">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-background border-border">
                  <SelectItem value="pending">pending</SelectItem>
                  <SelectItem value="in_progress">in_progress</SelectItem>
                  <SelectItem value="completed">completed</SelectItem>
                  <SelectItem value="cancelled">cancelled</SelectItem>
                  <SelectItem value="failed">failed</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-task-priority" className="text-sm text-muted-foreground">Priority</Label>
              <Select value={editPriority} onValueChange={(v) => setEditPriority(v as Task['priority'])}>
                <SelectTrigger id="edit-task-priority" className="w-full bg-background border-border text-foreground">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-background border-border">
                  <SelectItem value="low">low</SelectItem>
                  <SelectItem value="medium">medium</SelectItem>
                  <SelectItem value="high">high</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="edit-task-assigned" className="text-sm text-muted-foreground">Assigned to</Label>
            {/*
              The shared <AgentSelect> is backed by
              data-store::getActiveAgents (live-only) — the previous local
              <Select> populated by the unfiltered apiClient.getAgents
              endpoint leaked terminated ghost agents into the dropdown.
              noneLabel="— Unassigned —" matches CreateTaskModal so both
              forms speak the same nullable-assignment language.
            */}
            <AgentSelect
              id="edit-task-assigned"
              value={editAssignedTo}
              onChange={setEditAssignedTo}
              noneLabel="— Unassigned —"
            />
          </div>
          {/*
            Add-comment section. Append-only — the backend appends a new
            {timestamp, author, content} entry to the JSON notes array;
            we cannot edit/delete historical comments per-id (no PK in the
            schema). Leaving the textarea empty skips the notes field in
            the patch. The existing-comments preview below is read-only and
            gives the admin context for the new comment they're typing.
          */}
          <div className="border-t border-border pt-4 space-y-2">
            <Label htmlFor="edit-task-comment" className="text-sm text-muted-foreground">
              Add comment
            </Label>
            <Textarea
              id="edit-task-comment"
              value={editComment}
              onChange={(e) => setEditComment(e.target.value)}
              placeholder="Optional. Appended to the task comments log with your admin id and a timestamp."
              className="w-full bg-background border-border text-foreground min-h-[60px] whitespace-pre-wrap text-sm"
            />
            {existingComments.length > 0 && (
              <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer hover:text-foreground">
                  Existing comments ({existingComments.length})
                </summary>
                <div className="mt-2 space-y-2 max-h-[20vh] overflow-y-auto">
                  {existingComments.map((comment, idx: number) => (
                    <div key={idx} className="bg-muted/40 rounded p-2">
                      <div className="flex items-center justify-between mb-1">
                        <span className={cn(
                          "font-medium",
                          isTombstone(comment.author) && "italic"
                        )}>
                          {comment.author || 'unknown'}
                        </span>
                        <span title={comment.timestamp}>
                          {formatRelative(comment.timestamp)}
                        </span>
                      </div>
                      <p className="whitespace-pre-wrap text-foreground">{comment.content}</p>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
          {/* Monospace task_id footer — matches the View dialog idiom. */}
          <div className="border-t border-border pt-3 flex justify-between gap-2 text-xs text-muted-foreground">
            <span>Task ID</span>
            <span className="font-mono text-xs break-all">{task.task_id}</span>
          </div>
        </>
      )}
    </FormDialog>
  )
}
