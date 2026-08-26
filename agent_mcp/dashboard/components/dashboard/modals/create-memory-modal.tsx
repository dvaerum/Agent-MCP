"use client"

import React, { useState } from 'react'
import { Plus } from 'lucide-react'
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
  DialogTrigger,
} from '@/components/ui/dialog'
import { SmartValueEditor } from './smart-value-editor'
import { useContextRows } from '@/lib/queries/all-data'

interface CreateMemoryData {
  context_key: string
  context_value: unknown
  description?: string
}

interface CreateMemoryModalProps {
  onCreateMemory: (data: CreateMemoryData) => Promise<void>
  trigger?: React.ReactNode
}

export function CreateMemoryModal({ onCreateMemory, trigger }: CreateMemoryModalProps) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [formData, setFormData] = useState<{
    context_key: string
    context_value: unknown
    description: string
  }>({
    context_key: '',
    context_value: '',
    description: ''
  })

  // UX-10: suggestion chips must reflect the project's REAL existing
  // memory keys (pulled from the shared data store) so clicking one
  // reuses / edits an actual key, rather than inserting a hardcoded
  // fake example that doesn't exist. Typing a brand-new key free-text
  // stays fully supported.
  const context = useContextRows()
  const existingKeys = React.useMemo(() => {
    const keys = ((context ?? []) as Array<{ context_key?: string }>)
      .map((c) => c?.context_key)
      .filter((k): k is string => typeof k === 'string' && k.length > 0)
    // De-dupe, sort, and cap so a large bank doesn't flood the modal.
    return Array.from(new Set(keys)).sort().slice(0, 12)
  }, [context])

  const handleValueChange = (value: unknown) => {
    setFormData(prev => ({ ...prev, context_value: value }))
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    
    if (!formData.context_key.trim()) {
      return
    }

    setLoading(true)
    try {
      await onCreateMemory({
        context_key: formData.context_key.trim(),
        context_value: formData.context_value,
        description: formData.description.trim() || undefined
      })

      // Reset form and close modal
      setFormData({ context_key: '', context_value: '', description: '' })
      setOpen(false)
    } catch {
      // AX-2: the parent (handleCreateMemory in memories-dashboard)
      // already surfaces both outcomes through the shared toast —
      // toastSuccess on create, toastError on failure — and rethrows.
      // Those toasts are role="status"/"alert" + aria-live, so the
      // result is announced accessibly. We only need to keep the dialog
      // open on failure so the admin can retry (matches EditMemoryModal).
    } finally {
      setLoading(false)
    }
  }

  const handleCancel = () => {
    setFormData({ context_key: '', context_value: '', description: '' })
    setOpen(false)
  }

  const defaultTrigger = (
    <Button size="sm" className="bg-primary hover:bg-primary/90 text-primary-foreground shadow-lg hover:shadow-primary/25 transition-all duration-200">
      <Plus className="h-4 w-4 mr-1.5" />
      New Memory
    </Button>
  )

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger || defaultTrigger}
      </DialogTrigger>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-lg bg-card border-border text-card-foreground max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="text-lg">Create New Memory</DialogTitle>
          <DialogDescription className="text-muted-foreground">
            Add a new memory entry to the context bank. Use structured keys for better organization.
          </DialogDescription>
        </DialogHeader>
        
        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Context Key */}
          <div className="space-y-2">
            <Label htmlFor="context_key" className="text-sm font-medium text-foreground">
              Memory Key
            </Label>
            <Input
              id="context_key"
              value={formData.context_key}
              onChange={(e) => setFormData(prev => ({ ...prev, context_key: e.target.value }))}
              placeholder="e.g., api.config.base_url"
              className="bg-background border-border text-foreground font-mono text-sm"
              required
            />
            <div className="text-xs text-muted-foreground">
              Use dot notation for hierarchical organization (e.g., api.endpoints.users)
            </div>
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

          {/* Existing keys helper (UX-10). Only rendered when the
              project actually has memory keys — no fake examples. */}
          {existingKeys.length > 0 && (
            <div className="bg-muted/30 border border-border rounded-lg p-3">
              <div className="text-xs font-medium text-foreground mb-2">Existing keys (click to reuse or edit):</div>
              <div className="flex flex-wrap gap-1">
                {existingKeys.map((key) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setFormData(prev => ({ ...prev, context_key: key }))}
                    className="text-xs bg-background hover:bg-muted border border-border rounded px-2 py-1 text-muted-foreground hover:text-foreground transition-colors font-mono"
                  >
                    {key}
                  </button>
                ))}
              </div>
            </div>
          )}

          <DialogFooter className="gap-2 pt-4">
            <Button 
              type="button" 
              variant="outline" 
              onClick={handleCancel} 
              size="sm"
              disabled={loading}
            >
              Cancel
            </Button>
            <Button 
              type="submit" 
              size="sm" 
              disabled={loading || !formData.context_key.trim()}
              className="bg-primary hover:bg-primary/90 shadow-lg hover:shadow-primary/25 transition-all"
            >
              {loading ? (
                <>
                  <div className="h-3 w-3 border-2 border-primary-foreground border-t-transparent rounded-full animate-spin mr-2" />
                  Creating...
                </>
              ) : (
                <>
                  <Plus className="h-3 w-3 mr-2" />
                  Create Memory
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}