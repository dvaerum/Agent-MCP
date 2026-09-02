"use client"

import * as React from "react"
import { useEffect, useState } from "react"
import { apiClient } from "@/lib/api"
import { toastError } from "@/components/ui/toast"
import { DeleteConfirmModal } from "@/components/dashboard/modals/delete-confirm-modal"

export type PurgePreview = Awaited<ReturnType<typeof apiClient.getPurgePreview>>

/**
 * Blast-radius preview for a purge, rendered into the shared
 * `<DeleteConfirmModal>`'s `details` slot.
 *
 * The preview is a READ, so its failure stays inline (it is this
 * dialog's own list-load error, the same category the page scaffold
 * renders inline rather than toasting). Purge failures — a MUTATION —
 * surface through the shared toast plus the modal's inline error.
 */
function PurgePreviewBlock({
  agentId,
  preview,
  loading,
  error,
}: {
  agentId: string | null
  preview: PurgePreview | null
  loading: boolean
  error: string | null
}): React.ReactElement {
  return (
    <div className="space-y-2 text-sm">
      <div className="text-muted-foreground">
        Every reference to <code className="font-mono">{agentId ?? ''}</code> is
        rewritten to{' '}
        <code className="font-mono">
          {preview?.tombstone ?? `[deleted-${agentId ?? ''}]`}
        </code>
        . Task comments are preserved as an audit trail.
      </div>
      {loading && (
        <div className="text-sm text-muted-foreground py-2">
          Loading preview...
        </div>
      )}
      {error && <div className="text-sm text-destructive py-1">{error}</div>}
      {preview && (
        <>
          <div className="font-medium">This will tombstone:</div>
          <ul className="list-disc pl-5 space-y-1 text-muted-foreground">
            <li>
              {preview.counts.messages_sent} messages sent
              {preview.samples.messages_sent[0] && (
                <span className="text-xs block">
                  (last: &lsquo;{preview.samples.messages_sent[0].content}&rsquo;)
                </span>
              )}
            </li>
            <li>{preview.counts.messages_received} messages received</li>
            <li>
              {preview.counts.tasks_created} tasks created
              {preview.samples.tasks_created[0] && (
                <span className="text-xs block">
                  (e.g. &lsquo;{preview.samples.tasks_created[0]}&rsquo;)
                </span>
              )}
            </li>
            <li>
              {preview.counts.tasks_assigned} tasks assigned
              {preview.counts.tasks_assigned > 0 && (
                <span className="text-xs block">
                  (will be set to unassigned)
                </span>
              )}
            </li>
            <li>{preview.counts.agent_actions} agent_actions entries</li>
          </ul>
        </>
      )}
    </div>
  )
}

/**
 * Purge confirmation — hard delete + cascade tombstone. **Tier 3**.
 *
 * Routed through the shared `<DeleteConfirmModal>` (architecture review
 * Class 5: the pre-extraction dialog was the third hand-rolled variant
 * of the same type-to-confirm delete state machine, and the only one of
 * the three with NO type-to-confirm gate at all). Purge is the page's
 * one genuinely irreversible action, which is exactly what that modal's
 * "Permanent Data Loss Warning" contract is for; the blast-radius
 * preview it used to render itself becomes the modal's `details` slot.
 *
 * The confirmation word is the AGENT ID, case-sensitive — not `DELETE`.
 * There is no un-purge endpoint (Restore only reverses a *terminate*),
 * and agent ids are visually near-identical (`agent-a959a84c…` vs
 * `agent-a92d2d9ef…`) while terminated rows render interleaved with
 * active ones. Typing `DELETE` proves INTENT but not TARGET; typing the
 * id proves both, and is the only gate that catches "I purged the wrong
 * one" — the failure mode this dialog actually has.
 *
 * DO NOT collapse this back to a uniform `DELETE`. Users type the
 * username, groups the group name, projects the project name, purge the
 * agent id: each page demands a DIFFERENT string, so the gesture cannot
 * become a reflex (Anderson et al.'s polymorphic-warning effect). A
 * uniform word is one muscle-memory sequence that opens every tier-3
 * gate in the product.
 */
export function PurgeAgentDialog({
  agentId,
  open,
  onOpenChange,
  onConfirmed,
}: {
  agentId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirmed: () => void
}): React.ReactElement {
  const [preview, setPreview] = useState<PurgePreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !agentId) {
      setPreview(null)
      setPreviewError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setPreviewError(null)
    apiClient
      .getPurgePreview(agentId)
      .then((p) => {
        if (!cancelled) setPreview(p)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setPreviewError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, agentId])

  return (
    <DeleteConfirmModal
      open={open}
      onOpenChange={onOpenChange}
      entityLabel="Agent"
      requiredWord={agentId ?? ''}
      matchCase
      title={`Purge agent ${agentId ?? ''}?`}
      description="This deletes the agent row and tombstones every reference to it. Task comments are preserved as an audit trail."
      warningText="The agent row is deleted outright and every message, task and action that referenced it is rewritten to a tombstone. This action cannot be reversed."
      confirmLabel="Confirm purge"
      inputId="purge-confirmation"
      details={
        <PurgePreviewBlock
          agentId={agentId}
          preview={preview}
          loading={loading}
          error={previewError}
        />
      }
      onConfirm={async () => {
        if (!agentId) return
        try {
          await apiClient.purgeAgent(agentId)
          onConfirmed()
        } catch (e: unknown) {
          toastError(e, `Failed to purge ${agentId}`)
          // Re-throw so the shared modal keeps itself open and shows the
          // inline error next to the confirm input.
          throw e
        }
      }}
    />
  )
}
