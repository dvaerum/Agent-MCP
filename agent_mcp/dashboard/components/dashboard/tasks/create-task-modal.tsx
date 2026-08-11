"use client"

import { useState } from "react"
import { Plus } from "lucide-react"
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
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"

export interface CreateTaskModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Refresh the list after a successful create. */
  onCreated: () => void
}

/**
 * Create task — Wave 5: adopts the shared `<FormDialog>` +
 * `useAsyncSubmit` shell (mirrors messages' compose). Parent-controlled
 * open (the header + empty-state "Create Task" buttons drive it); the
 * shell owns the chrome + Cancel/Create footer + success/error toast +
 * close-on-success.
 */
export function CreateTaskModal({ open, onOpenChange, onCreated }: CreateTaskModalProps) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [priority, setPriority] = useState<Task['priority']>('medium')
  // null = "— Unassigned —" sentinel selected (no assignment). The shared
  // <AgentSelect> surfaces the live agent roster instead of asking the
  // admin to type an agent_id.
  const [assignedTo, setAssignedTo] = useState<string | null>(null)

  const reset = () => {
    setTitle('')
    setDescription('')
    setPriority('medium')
    setAssignedTo(null)
  }

  // The create mutation. Throws on failure so the shared shell keeps the
  // dialog open with the operator's draft intact + surfaces the toast.
  const handleCreate = async () => {
    if (!title.trim()) return
    await apiClient.createTask({
      title: title.trim(),
      description: description.trim() || undefined,
      priority,
      // AgentSelect returns string | null — the null sentinel maps to
      // "no assignment", which the create-task endpoint accepts as
      // undefined / missing.
      assigned_to: assignedTo ?? undefined,
    })
    onCreated()
    reset()
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Create Task"
      description="Define a new task for the system to execute."
      icon={Plus}
      onSubmit={handleCreate}
      submitLabel="Create Task"
      submittingLabel="Creating…"
      submitDisabled={!title.trim()}
      successMessage="Task created."
      errorMessage="Failed to create task"
    >
      <div className="space-y-2">
        <Label htmlFor="create-task-title" className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          Task Title
        </Label>
        <Input
          id="create-task-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Analyze dataset and generate report"
          className="bg-background border-border text-foreground"
          required
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="create-task-description" className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          Description
        </Label>
        <Textarea
          id="create-task-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Detailed task requirements and objectives..."
          className="bg-background border-border text-foreground h-20 resize-none"
        />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label htmlFor="create-task-priority" className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Priority
          </Label>
          <Select value={priority} onValueChange={(value: Task['priority']) => setPriority(value)}>
            <SelectTrigger id="create-task-priority" className="bg-background border-border text-foreground">
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="bg-background border-border">
              <SelectItem value="low">Low</SelectItem>
              <SelectItem value="medium">Medium</SelectItem>
              <SelectItem value="high">High</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="create-task-assigned" className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Assign To
          </Label>
          {/*
            The shared <AgentSelect> sources live agents from the
            data-store (filters terminated rows via shouldDisplayAgent)
            and pins Admin at the top. noneLabel="— Unassigned —" because
            the underlying field is a nullable assignment, not a filter.
          */}
          <AgentSelect
            id="create-task-assigned"
            value={assignedTo}
            onChange={setAssignedTo}
            noneLabel="— Unassigned —"
          />
        </div>
      </div>
    </FormDialog>
  )
}
