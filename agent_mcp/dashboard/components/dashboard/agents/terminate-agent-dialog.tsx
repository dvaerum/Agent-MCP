"use client"

import * as React from "react"
import { PowerOff } from "lucide-react"
import { toastError } from "@/components/ui/toast"
import { ConfirmActionModal } from "@/components/dashboard/modals/confirm-action-modal"

/**
 * Terminate confirmation — soft-delete only. **Tier 1**.
 *
 * Deliberately NOT the type-to-confirm `<DeleteConfirmModal>`:
 * terminate is reversible (the row, its tasks and its messages survive;
 * Restore and Purge both remain available afterwards), so the
 * type-DELETE-to-arm gate would be miscalibrated. It IS disruptive
 * enough to deserve a confirm that names the agent — which is exactly
 * tier 1. The irreversible sibling — Purge — is tier 3 (type the agent
 * id, see `purge-agent-dialog.tsx`).
 *
 * The dialog body is the shared `<ConfirmActionModal>`; this component
 * keeps only the terminate-specific copy and the toast-plus-re-throw
 * error policy. The icon is overridden away from the default trash can:
 * nothing is being deleted here.
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
  return (
    <ConfirmActionModal
      open={open}
      onOpenChange={onOpenChange}
      icon={PowerOff}
      title={`Terminate agent ${agentId ?? ''}?`}
      description={
        <>
          This is a soft-delete. The agent&apos;s row, tasks, and messages
          are preserved — you can Restore it or Purge it (hard delete +
          tombstone) from the terminated-row actions after.
        </>
      }
      confirmLabel="Terminate"
      busyLabel="Terminating..."
      onConfirm={async () => {
        if (!agentId) return
        try {
          await onConfirmed(agentId)
        } catch (e: unknown) {
          // The page's mutation handler already toasts; this is the
          // last-resort surface for a rejection that reaches us anyway.
          toastError(e, `Failed to terminate ${agentId}`)
          // Re-throw so the shared modal stays open with the reason
          // inline and the operator can retry.
          throw e
        }
      }}
    />
  )
}
