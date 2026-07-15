"use client"

import React, { useState } from 'react'
import { Pencil } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { SmartValueEditor } from './smart-value-editor'
import type { Memory } from '@/lib/api'

interface EditMemoryData {
  context_key: string
  context_value: any
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
export function EditMemoryModal({ memory, open, onOpenChange, onUpdateMemory }: EditMemoryModalProps) {
  const [loading, setLoading] = useState(false)
  const [formData, setFormData] = useState({
    context_key: memory.context_key,
    context_value: memory.value,
    description: memory.description || ''
  })

  const memoryKey = memory?.context_key
  React.useEffect(() => {
    if (open && memory) {
      setFormData({
        context_key: memory.context_key,
        context_value: memory.value,
        description: memory.description || ''
      })
    }
    // Deliberately keyed on memoryKey, not memory — see comment above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, memoryKey])

  const handleValueChange = (value: any) => {
    setFormData(prev => ({ ...prev, context_value: value }))
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()

    setLoading(true)
    try {
      await onUpdateMemory({
        context_key: formData.context_key,
        context_value: formData.context_value,
        description: formData.description.trim() || undefined
      })
      // Self-close on success — matches EditTaskDialog. On failure the
      // parent surfaces a toast and we keep the dialog open (the throw
      // below lands in the catch).
      onOpenChange(false)
    } catch (error) {
      // Parent (handleUpdateMemory) already surfaced the error via
      // toastError; keep the dialog open so the admin can retry.
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-lg bg-card border-border text-card-foreground max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="text-lg">Edit Memory</DialogTitle>
          <DialogDescription className="text-muted-foreground">
            Update the memory entry. The key cannot be changed.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Context Key (Read-only) */}
          <div className="space-y-2">
            <Label htmlFor="context_key" className="text-sm font-medium text-foreground">
              Memory Key (Read-only)
            </Label>
            <Input
              id="context_key"
              value={formData.context_key}
              disabled
              className="bg-muted/50 border-border text-muted-foreground font-mono text-sm"
            />
          </div>

          {/* Context Value */}
          <div className="space-y-2">
            <Label className="text-sm font-medium text-foreground">
              Memory Value
            </Label>
            <SmartValueEditor
              value={formData.context_value}
              onChange={handleValueChange}
              className="border rounded-lg p-3 bg-background"
            />
          </div>

          {/* Description */}
          <div className="space-y-2">
            <Label htmlFor="description" className="text-sm font-medium text-foreground">
              Description (Optional)
            </Label>
            <Textarea
              id="description"
              value={formData.description}
              onChange={(e) => setFormData(prev => ({ ...prev, description: e.target.value }))}
              placeholder="Brief description of what this memory stores..."
              className="bg-background border-border text-foreground h-20 resize-none"
              rows={3}
            />
          </div>

          <DialogFooter className="gap-2 pt-4">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              size="sm"
              disabled={loading}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              size="sm"
              disabled={loading}
              className="bg-primary hover:bg-primary/90 shadow-lg hover:shadow-primary/25 transition-all"
            >
              {loading ? (
                <>
                  <div className="h-3 w-3 border-2 border-primary-foreground border-t-transparent rounded-full animate-spin mr-2" />
                  Updating...
                </>
              ) : (
                <>
                  <Pencil className="h-3 w-3 mr-2" />
                  Update Memory
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
