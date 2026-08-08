"use client"

import * as React from "react"
import { useState } from "react"
import { ConfirmActionModal } from "@/components/dashboard/modals/confirm-action-modal"

interface ServerLike {
  id: string
  name: string
}

/**
 * Tier-1 confirmations for the two server-picker surfaces.
 *
 * `server-connection.tsx` and `server-management-modal.tsx` are
 * near-duplicates of each other, and between them carried SIX native
 * `window.confirm()` calls (four "Delete server", two "clear all
 * configs"). A native confirm sits outside the confirmation model
 * entirely: no `role="alertdialog"`, no theming, not visible to any of
 * the dialog audits, and it blocks the main thread. This hook is the
 * one place that wiring lives so the migration didn't add a seventh
 * copy.
 *
 * Both actions are **tier 1** under the model in
 * `modals/confirm-action-modal.tsx`. They mutate only the operator's
 * OWN browser storage — a list of name/host/port triples — and the
 * "Add New Server" form is on the same screen, so the state is
 * recreatable in seconds. Neither is unrecoverable, which is what
 * tier 2 requires; "clear all" is bulk but bulk alone does not
 * escalate. What they DO need, and native confirm never gave them
 * consistently, is naming the target.
 */
export function useServerConfirms({
  removeServer,
  clearPersistedData,
  serverCount,
}: {
  removeServer: (id: string) => void
  clearPersistedData: () => void
  serverCount: number
}): {
  requestRemove: (server: ServerLike) => void
  requestClear: () => void
  confirmModals: React.ReactNode
} {
  const [pendingRemove, setPendingRemove] = useState<ServerLike | null>(null)
  const [clearOpen, setClearOpen] = useState(false)

  const confirmModals = (
    <>
      {pendingRemove && (
        <ConfirmActionModal
          open
          onOpenChange={(o) => {
            if (!o) setPendingRemove(null)
          }}
          title="Remove server"
          description={`Remove “${pendingRemove.name}” from this browser's saved servers? The server itself is untouched — you can add it back from the form on this page.`}
          confirmLabel="Remove server"
          busyLabel="Removing…"
          onConfirm={() => {
            removeServer(pendingRemove.id)
          }}
        />
      )}
      <ConfirmActionModal
        open={clearOpen}
        onOpenChange={setClearOpen}
        title="Clear saved servers"
        description={`Clear all ${serverCount} saved server configurations from this browser and reset to defaults? No server is stopped or deleted — only this browser forgets how to reach them.`}
        confirmLabel="Clear all"
        busyLabel="Clearing…"
        onConfirm={() => {
          clearPersistedData()
        }}
      />
    </>
  )

  return {
    requestRemove: setPendingRemove,
    requestClear: () => setClearOpen(true),
    confirmModals,
  }
}
