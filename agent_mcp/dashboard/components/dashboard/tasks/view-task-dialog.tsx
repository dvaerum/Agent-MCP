"use client"

import { Pencil, Trash2, GitBranch } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { type Task } from "@/lib/api"
import { cn, formatRelative } from "@/lib/utils"
import {
  statusBadgeClass,
  priorityBadgeClass,
  isTombstone,
  parseTaskNotes,
} from "@/components/dashboard/tasks/tasks-api"

// =========================================================================
// Row-action dialogs: View / Edit / Delete
//
// Each opens a shadcn Dialog (NOT a sidebar Sheet). They mirror the
// Messages-page detail popup pattern from PR #36 and the agents-page UI.
// =========================================================================

export interface ViewTaskDialogProps {
  task: Task | null
  onOpenChange: (open: boolean) => void
  // Optional in-modal actions. When provided, the parent wires these to
  // close this view dialog and open the sibling edit/delete confirm
  // dialog (close-then-open avoids stacked-dialog issues).
  onEdit?: () => void
  onDelete?: () => void
}

export function ViewTaskDialog({ task, onOpenChange, onEdit, onDelete }: ViewTaskDialogProps) {
  const open = task !== null

  // W4-followup(A): `child_tasks` / `depends_on_tasks` are normalized to
  // `string[]` at the lib/api boundary (`normalizeTask`), so we consume
  // them directly — no more per-component defensive parse. `notes` is
  // NOT normalized there, so it keeps a small typed parse.
  const dependencies = task?.depends_on_tasks ?? []
  const childTasks = task?.child_tasks ?? []
  const notes = task ? parseTaskNotes(task.notes) : []
  const createdBy: string | undefined = task?.created_by

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        Width: sm:!max-w-3xl overrides the base DialogContent's `sm:max-w-lg`
        (32rem = 512px) which otherwise wins the cascade and squeezes the
        dialog to a phone-narrow 512px on desktop. The `!` (Tailwind important)
        is required because both classes share the same specificity and the
        base class is declared later in the merged className string.
      */}
      <DialogContent className="sm:!max-w-3xl w-[calc(100vw-2rem)] bg-card border-border text-card-foreground p-0 gap-0 max-h-[90vh] flex flex-col">
        {task && (
          <>
            <DialogHeader className="px-6 pt-6 pb-4 border-b border-border flex-shrink-0">
              <DialogTitle className="flex items-start justify-between pr-8 gap-3">
                {/*
                  Title wraps onto multiple lines (max 3) rather than being
                  silently cut off. `break-words` so a long unbroken
                  task_1780… style id still wraps instead of overflowing.
                */}
                <span className="text-lg font-semibold break-words line-clamp-3 leading-snug">{task.title}</span>
                <div className="flex items-center gap-2 flex-shrink-0 pt-0.5">
                  <Badge variant="outline" className={cn("text-xs", statusBadgeClass(task.status))}>
                    {task.status.replace(/_/g, ' ')}
                  </Badge>
                  <Badge variant="outline" className={cn("text-xs", priorityBadgeClass(task.priority))}>
                    {task.priority}
                  </Badge>
                </div>
              </DialogTitle>
              <DialogDescription className="text-muted-foreground">
                Read-only view of every task field. Use the pencil icon on the row to edit.
              </DialogDescription>
            </DialogHeader>

            {/*
              Scrollable body: parent is now `flex-col` with the header +
              footer marked flex-shrink-0, so this `flex-1 min-h-0 overflow-y-auto`
              expands to fill remaining space and is the single scroll region.
              Previously `max-h-[80vh]` on the body alone could push the dialog
              past the viewport (we observed h=984 on a 1000px viewport for a
              65k-char description), so the dialog now caps at 90vh total.
            */}
            <div className="px-6 py-4 flex-1 min-h-0 overflow-y-auto space-y-4 text-sm">
              {/* Group 1: core metadata in a 2-col grid */}
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label className="text-xs text-muted-foreground uppercase tracking-wider">Status</Label>
                  <div>
                    <Badge variant="outline" className={cn("text-xs", statusBadgeClass(task.status))}>
                      {task.status.replace(/_/g, ' ')}
                    </Badge>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label className="text-xs text-muted-foreground uppercase tracking-wider">Priority</Label>
                  <div>
                    <Badge variant="outline" className={cn("text-xs", priorityBadgeClass(task.priority))}>
                      {task.priority}
                    </Badge>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label className="text-xs text-muted-foreground uppercase tracking-wider">Assigned to</Label>
                  <div className={cn("text-sm", !task.assigned_to && "text-muted-foreground italic")}>
                    {task.assigned_to || '(unassigned)'}
                  </div>
                </div>
                {createdBy && (
                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground uppercase tracking-wider">Created by</Label>
                    <div className={cn(
                      "text-sm",
                      isTombstone(createdBy) && "text-muted-foreground italic"
                    )}>
                      {createdBy}
                    </div>
                  </div>
                )}
                {task.parent_task && (
                  <div className="space-y-2 col-span-2">
                    <Label className="text-xs text-muted-foreground uppercase tracking-wider">Parent task</Label>
                    <div>
                      <Badge variant="outline" className="text-xs font-mono">
                        <GitBranch className="h-3 w-3 mr-1" />
                        {task.parent_task}
                      </Badge>
                    </div>
                  </div>
                )}
              </div>

              {/*
                Group 2: description.
                - `[overflow-wrap:anywhere]` so a 65k-char unbroken string
                  (we have one in the wild — `XXX…XXX`) wraps inside the
                  block instead of forcing the body to a giant scroll-X.
                - NO inner `max-h-[Nvh] overflow-y-auto`. The dialog body
                  (`max-h-[90vh]` + `flex-1 min-h-0 overflow-y-auto`) is
                  the single vertical scroll region; nesting another one
                  here forced users to scroll twice (PR #54's polish
                  over-corrected for monster bodies). Long descriptions
                  now flow naturally into the body scroll alongside the
                  metadata footer.
              */}
              <div className="border-t border-border pt-4 space-y-2">
                <Label className="text-xs text-muted-foreground uppercase tracking-wider">Description</Label>
                {task.description ? (
                  <pre className="text-sm whitespace-pre-wrap break-words [overflow-wrap:anywhere] font-mono text-xs leading-relaxed bg-muted/40 rounded p-3">
                    {task.description}
                  </pre>
                ) : (
                  <p className="text-sm text-muted-foreground italic">(no description)</p>
                )}
              </div>

              {/* Group 3: relations (only renders if present) */}
              {(dependencies.length > 0 || childTasks.length > 0) && (
                <div className="border-t border-border pt-4 space-y-4">
                  {dependencies.length > 0 && (
                    <div className="space-y-2">
                      <Label className="text-xs text-muted-foreground uppercase tracking-wider">Depends on</Label>
                      <div className="flex flex-wrap gap-2">
                        {dependencies.map((id, idx) => (
                          <Badge key={idx} variant="outline" className="text-xs font-mono">{String(id)}</Badge>
                        ))}
                      </div>
                    </div>
                  )}
                  {childTasks.length > 0 && (
                    <div className="space-y-2">
                      <Label className="text-xs text-muted-foreground uppercase tracking-wider">Subtasks</Label>
                      <div className="flex flex-wrap gap-2">
                        {childTasks.map((id, idx) => (
                          <Badge key={idx} variant="outline" className="text-xs font-mono">{String(id)}</Badge>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/*
                Group 4: notes — always renders, with an empty state
                when the task has none. Gating on `notes.length > 0`
                hid the section completely for empty tasks (no
                affordance, no hint the feature exists). The Add-note
                affordance lives in the Edit dialog (`apiClient.updateTask`
                with `notes: string` appends a new entry).
              */}
              <div className="border-t border-border pt-4 space-y-2">
                <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                  Notes{notes.length > 0 ? ` (${notes.length})` : ''}
                </Label>
                {notes.length > 0 ? (
                  <div className="space-y-2">
                    {notes.map((note, idx) => (
                      <div key={idx} className="bg-muted/50 rounded-lg p-3">
                        <div className="flex items-center justify-between mb-1 text-xs">
                          <span className={cn(
                            "font-medium",
                            isTombstone(note.author) && "text-muted-foreground italic"
                          )}>
                            {note.author || 'unknown'}
                          </span>
                          <span className="text-muted-foreground" title={note.timestamp}>
                            {formatRelative(note.timestamp)}
                          </span>
                        </div>
                        <p className="text-sm whitespace-pre-wrap">{note.content}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground italic">
                    No notes yet. Use the Edit dialog to add one.
                  </p>
                )}
              </div>

              {/*
                Group 5: tombstone metadata footer.
                Each row is a 2-col grid (label / value) so the value is
                always right-aligned with `text-right` and `break-all`
                allows long ISO timestamps + " · 3h ago" or a long
                task_id to wrap cleanly instead of overflowing the modal
                at narrow widths.
              */}
              <div className="border-t border-border pt-4 space-y-1 text-xs text-muted-foreground">
                <div className="grid grid-cols-[6rem_1fr] gap-2">
                  <span>Created</span>
                  <span className="font-mono text-xs break-all text-right" title={task.created_at}>
                    {task.created_at} · {formatRelative(task.created_at)}
                  </span>
                </div>
                <div className="grid grid-cols-[6rem_1fr] gap-2">
                  <span>Updated</span>
                  <span className="font-mono text-xs break-all text-right" title={task.updated_at}>
                    {task.updated_at} · {formatRelative(task.updated_at)}
                  </span>
                </div>
                <div className="grid grid-cols-[6rem_1fr] gap-2">
                  <span>Task ID</span>
                  <span className="font-mono text-xs break-all text-right">{task.task_id}</span>
                </div>
              </div>
            </div>

            <DialogFooter className="px-6 py-4 border-t border-border flex-shrink-0">
              {onEdit && (
                <Button variant="outline" size="sm" onClick={onEdit}>
                  <Pencil className="h-4 w-4 mr-1" />
                  Edit
                </Button>
              )}
              {onDelete && (
                <Button variant="destructive" size="sm" onClick={onDelete}>
                  <Trash2 className="h-4 w-4 mr-1" />
                  Delete
                </Button>
              )}
              <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
