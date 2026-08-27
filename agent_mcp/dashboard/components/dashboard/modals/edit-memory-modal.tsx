"use client"

import { useEffect, useState } from "react"
import { Pencil } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import { SmartValueEditor } from "./smart-value-editor"
import type { Memory } from "@/lib/api"

interface EditMemoryData {
  context_key: string
  context_value: unknown
  description?: string
}

interface EditMemoryModalProps {
  memory: Memory
  open: boolean
  onOpenChange: (open: boolean) => void
  onUpdateMemory: (data: EditMemoryData) => Promise<void>
}

// Extracted from memories-dashboard.tsx (was an inline component) to
// match the tasks page's EditTaskDialog-as-standalone-component shape.
// Keeps the SmartValueEditor value-editing (the recent good work) and
// re-seeds the form only on a *different* memory (key change) so a
// background refresh can't clobber the admin's in-progress edits —
// live-lookup useDialog (Candidate D, 2026-06-02).
//
// Adopts the shared <FormDialog> shell (mobile dvh-cap + scrollable
// body). `onUpdateMemory` (the parent's handleUpdateMemory) already
// toasts both outcomes, so FormDialog's own successMessage/
// errorMessage are omitted — the shell just owns the open/close +
// stay-open-on-error mechanics; `onUpdateMemory` throwing is what
// keeps it open.
export function EditMemoryModal({ memory, open, onOpenChange, onUpdateMemory }: EditMemoryModalProps) {
  const [formData, setFormData] = useState({
    context_key: memory.context_key,
    context_value: memory.value,
    description: memory.description || "",
  })

  const memoryKey = memory?.context_key
  useEffect(() => {
    if (open && memory) {
      setFormData({
        context_key: memory.context_key,
        context_value: memory.value,
        description: memory.description || "",
      })
    }
    // Deliberately keyed on memoryKey, not memory — see comment above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, memoryKey])

  const handleValueChange = (value: unknown) => {
    setFormData((prev) => ({ ...prev, context_value: value }))
  }

  const handleSubmit = async () => {
    await onUpdateMemory({
      context_key: formData.context_key,
      context_value: formData.context_value,
      description: formData.description.trim() || undefined,
    })
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Edit Memory"
      description="Update the memory entry. The key cannot be changed."
      icon={Pencil}
      onSubmit={handleSubmit}
      submitLabel="Update Memory"
      submittingLabel="Updating…"
    >
      <div className="space-y-2">
        <Label htmlFor="context_key">Memory Key (Read-only)</Label>
        <Input
          id="context_key"
          value={formData.context_key}
          disabled
          className="bg-muted/50 font-mono text-sm text-muted-foreground"
        />
      </div>

      <div className="space-y-2">
        <Label>Memory Value</Label>
        <SmartValueEditor
          value={formData.context_value}
          onChange={handleValueChange}
          className="rounded-lg border bg-background p-3"
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="description">Description (Optional)</Label>
        <Textarea
          id="description"
          value={formData.description}
          onChange={(e) => setFormData((prev) => ({ ...prev, description: e.target.value }))}
          placeholder="Brief description of what this memory stores..."
          className="h-20 resize-none"
          rows={3}
        />
      </div>
    </FormDialog>
  )
}
