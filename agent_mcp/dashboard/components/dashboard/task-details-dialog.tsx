"use client"

import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import { Task } from '@/lib/api'
import { cn } from '@/lib/utils'
import { statusColorClasses, priorityColorClasses } from '@/lib/status'

interface TaskDetailsDialogProps {
  task: Task | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function TaskDetailsDialog({ task, open, onOpenChange }: TaskDetailsDialogProps) {
  if (!task) return null

  // Helper function to parse JSON fields safely
  const parseJsonField = (field: unknown): unknown[] => {
    if (!field) return []
    if (Array.isArray(field)) return field
    if (typeof field === 'string') {
      try {
        const parsed = JSON.parse(field)
        return Array.isArray(parsed) ? parsed : []
      } catch {
        return []
      }
    }
    return []
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-2xl max-h-[80vh]">
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between pr-8">
            <span className="text-lg font-semibold">{task.title}</span>
            <Badge variant="outline" className={cn("text-xs", statusColorClasses(task.status))}>
              {task.status.replace(/_/g, ' ')}
            </Badge>
          </DialogTitle>
          {/* AX-5: Radix warns (and screen readers get no accessible
              description) when DialogContent has no DialogDescription. */}
          <DialogDescription className="text-muted-foreground">
            Full details for this task — status, assignment, description, and dependencies.
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className="max-h-[60vh] pr-4">
          <div className="space-y-4">
            {/* Task Info */}
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <span className="text-muted-foreground">Task ID</span>
                <p className="font-mono text-xs mt-1">{task.task_id}</p>
              </div>
              <div>
                <span className="text-muted-foreground">Priority</span>
                <Badge variant="outline" className={cn("text-xs mt-1", priorityColorClasses(task.priority))}>
                  {task.priority}
                </Badge>
              </div>
              <div>
                <span className="text-muted-foreground">Assigned To</span>
                <p className="text-sm mt-1">{task.assigned_to || 'Unassigned'}</p>
              </div>
              <div>
                <span className="text-muted-foreground">Created</span>
                <p className="text-sm mt-1">{new Date(task.created_at).toLocaleDateString()}</p>
              </div>
            </div>

            <Separator />

            {/* Description */}
            {task.description && (
              <>
                <div>
                  <h4 className="text-sm font-semibold mb-2">Description</h4>
                  <p className="text-sm text-muted-foreground whitespace-pre-wrap">{task.description}</p>
                </div>
                <Separator />
              </>
            )}

            {/* Notes */}
            {(() => {
              const notes = parseJsonField(task.notes) as Array<{ author: string; timestamp: string; content: string }>
              return notes.length > 0 && (
                <div>
                  <h4 className="text-sm font-semibold mb-3">Notes</h4>
                  <div className="space-y-3">
                    {notes.map((note, index) => (
                      <div key={index} className="bg-muted rounded-lg p-3">
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-xs font-medium">{note.author}</span>
                          <span className="text-xs text-muted-foreground">
                            {new Date(note.timestamp).toLocaleString()}
                          </span>
                        </div>
                        <p className="text-sm">{note.content}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )
            })()}

            {/* Dependencies */}
            {(() => {
              const dependencies = parseJsonField(task.depends_on_tasks) as string[]
              return dependencies.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <h4 className="text-sm font-semibold mb-2">Dependencies</h4>
                    <div className="flex flex-wrap gap-2">
                      {dependencies.map((depId, index) => (
                        <Badge key={index} variant="outline" className="text-xs">
                          {depId}
                        </Badge>
                      ))}
                    </div>
                  </div>
                </>
              )
            })()}

            {/* Child Tasks */}
            {(() => {
              const childTasks = parseJsonField(task.child_tasks) as string[]
              return childTasks.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <h4 className="text-sm font-semibold mb-2">Subtasks</h4>
                    <div className="flex flex-wrap gap-2">
                      {childTasks.map((childId, index) => (
                        <Badge key={index} variant="outline" className="text-xs">
                          {childId}
                        </Badge>
                      ))}
                    </div>
                  </div>
                </>
              )
            })()}
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}