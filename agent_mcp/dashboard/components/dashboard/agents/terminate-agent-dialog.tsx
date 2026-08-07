"use client"

import * as React from "react"
import { useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { toastError } from "@/components/ui/toast"

/**
 * Terminate confirmation — soft-delete only.
 *
 * Deliberately NOT the shared `<DeleteConfirmModal>`: terminate is
 * reversible (the row, its tasks and its messages survive; Restore and
 * Purge both remain available afterwards), so the type-DELETE-to-arm
 * gate that modal exists to impose would be miscalibrated here. The
 * irreversible sibling — Purge — DOES use the shared modal (see
 * `purge-agent-dialog.tsx`).
 */
export function TerminateAgentDialog({
  agentId,
  open,
  onOpenChange,
  onConfirmed,
}: {
  agentId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirmed: (agentId: string) => Promise<void> | void
}): React.ReactElement {
  const [busy, setBusy] = useState(false)

  const handleConfirm = async () => {
    if (!agentId) return
    setBusy(true)
    try {
      await onConfirmed(agentId)
      onOpenChange(false)
    } catch (e: unknown) {
      // The page's mutation handler already toasts; this is the
      // last-resort surface for a rejection that reaches us anyway. The
      // dialog stays open so the operator can retry.
      toastError(e, `Failed to terminate ${agentId}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-md bg-card border-border text-card-foreground max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="text-lg">
            Terminate agent {agentId ?? ''}?
          </DialogTitle>
          <DialogDescription className="text-muted-foreground">
            This is a soft-delete. The agent&apos;s row, tasks, and messages
            are preserved — you can Restore it or Purge it (hard delete +
            tombstone) from the terminated-row actions after.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={handleConfirm}
            disabled={busy}
            className="bg-destructive hover:bg-destructive/90 text-destructive-foreground"
          >
            {busy ? 'Terminating...' : 'Terminate'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
