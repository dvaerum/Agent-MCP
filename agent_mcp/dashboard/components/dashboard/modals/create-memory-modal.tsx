"use client"

import { useMemo, useState } from "react"
import type { ReactElement } from "react"
import { Plus } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import { SmartValueEditor } from "./smart-value-editor"
import { useContextRows } from "@/lib/queries/all-data"

interface CreateMemoryData {
  context_key: string
  context_value: unknown
  description?: string
}

interface CreateMemoryModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreateMemory: (data: CreateMemoryData) => Promise<void>
}

/**
 * Create-memory modal — adopts the shared <FormDialog> shell. Externally
 * controlled (`open`/`onOpenChange`) rather than owning a `<DialogTrigger>`
 * — FormDialog owns its own `<Dialog>` root, which gives no slot for an
 * embedded trigger from a second, separate `<Dialog>` instance (Radix's
 * trigger/content coupling is context-based). The two mount points in
 * memories-dashboard.tsx (the header action + the empty-state CTA) are
 * now plain buttons that both open this single controlled dialog — same
 * external-button-triggers-`setOpen(true)` convention every other
 * FormDialog adopter already uses.
 *
 * `onCreateMemory` (the parent's handleCreateMemory) already surfaces
 * both outcomes through the shared toast (AX-2) and rethrows, so
 * FormDialog's own successMessage/errorMessage are omitted — the shell
 * just owns the open/close + stay-open-on-error mechanics.
 */
export function CreateMemoryModal({
  open,
  onOpenChange,
  onCreateMemory,
}: CreateMemoryModalProps): ReactElement {
  const [formData, setFormData] = useState<{
    context_key: string
    context_value: unknown
    description: string
  }>({
    context_key: "",
    context_value: "",
    description: "",
  })

  // UX-10: suggestion chips must reflect the project's REAL existing
  // memory keys (pulled from the shared data store) so clicking one
  // reuses / edits an actual key, rather than inserting a hardcoded
  // fake example that doesn't exist. Typing a brand-new key free-text
  // stays fully supported.
  const context = useContextRows()
  const existingKeys = useMemo(() => {
    const keys = ((context ?? []) as Array<{ context_key?: string }>)
      .map((c) => c?.context_key)
      .filter((k): k is string => typeof k === "string" && k.length > 0)
    // De-dupe, sort, and cap so a large bank doesn't flood the modal.
    return Array.from(new Set(keys)).sort().slice(0, 12)
  }, [context])

  const reset = () => {
    setFormData({ context_key: "", context_value: "", description: "" })
  }

  const handleValueChange = (value: unknown) => {
    setFormData((prev) => ({ ...prev, context_value: value }))
  }

  const handleSubmit = async () => {
    await onCreateMemory({
      context_key: formData.context_key.trim(),
      context_value: formData.context_value,
      description: formData.description.trim() || undefined,
    })
    reset()
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o)
        if (!o) reset()
      }}
      title="Create New Memory"
      description="Add a new memory entry to the context bank. Use structured keys for better organization."
      icon={Plus}
      onSubmit={handleSubmit}
      submitLabel="Create Memory"
      submittingLabel="Creating…"
      submitDisabled={!formData.context_key.trim()}
    >
      <div className="space-y-2">
        <Label htmlFor="context_key">Memory Key</Label>
        <Input
          id="context_key"
          value={formData.context_key}
          onChange={(e) => setFormData((prev) => ({ ...prev, context_key: e.target.value }))}
          placeholder="e.g., api.config.base_url"
          className="font-mono text-sm"
          required
        />
        <div className="text-xs text-muted-foreground">
          Use dot notation for hierarchical organization (e.g., api.endpoints.users)
        </div>
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

      {/* Existing keys helper (UX-10). Only rendered when the project
          actually has memory keys — no fake examples. */}
      {existingKeys.length > 0 && (
        <div className="rounded-lg border bg-muted/30 p-3">
          <div className="mb-2 text-xs font-medium text-foreground">Existing keys (click to reuse or edit):</div>
          <div className="flex flex-wrap gap-1">
            {existingKeys.map((key) => (
              <button
                key={key}
                type="button"
                onClick={() => setFormData((prev) => ({ ...prev, context_key: key }))}
                className="rounded border bg-background px-2 py-1 font-mono text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                {key}
              </button>
            ))}
          </div>
        </div>
      )}
    </FormDialog>
  )
}
