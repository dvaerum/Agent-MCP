"use client"

import * as React from "react"
import { useState } from "react"
import { Trash2, AlertTriangle, Lock } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"
import { onEnterSubmit } from "@/lib/keyboard"

/**
 * Generic type-to-confirm delete dialog.
 *
 * Unifies the two near-identical delete modals (delete-memory-modal /
 * delete-message-modal — architecture review Class 5): same
 * `{loading, confirmationText, error}` state machine, same
 * type-the-word-to-arm-the-button gate, same Enter-submits wiring
 * (`onEnterSubmit`), same warning banner + inline error. They differed
 * only in the entity noun and the per-entity preview block, which are
 * now the `entityLabel` prop and the `details` slot.
 *
 * The parent owns the actual delete call via `onConfirm`; it should
 * re-throw on failure so the dialog stays open and shows the inline
 * error (mutation handlers additionally surface a shared toast).
 *
 * Follow-up pages (messages single/bulk, users, groups) adopt this by
 * passing their preview markup as `details`, overriding `title` /
 * `description` for the bulk variant, and — for users/groups whose
 * confirmation word is a case-sensitive name — setting `requiredWord`
 * + `matchCase`.
 */
export interface DeleteConfirmModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Performs the delete. Re-throw to keep the dialog open on failure. */
  onConfirm: () => Promise<void>
  /** Entity noun, e.g. "Memory" — drives the default title/button/copy. */
  entityLabel: string
  /** Word the operator must type to arm the button. Default "DELETE". */
  requiredWord?: string
  /** Match the required word case-sensitively (for name-based confirms). */
  matchCase?: boolean
  /** Override the "Delete {entityLabel}" title. */
  title?: string
  /** Override the standard description line. */
  description?: string
  /** Override the warning banner heading. */
  warningTitle?: string
  /** Override the warning banner body. */
  warningText?: string
  /** Entity-specific preview block rendered above the confirm input. */
  details?: React.ReactNode
  /** Override the confirm button label (non-loading state). */
  confirmLabel?: string
  /** id for the confirm <input> (defaults to "confirmation"). */
  inputId?: string
}

export function DeleteConfirmModal({
  open,
  onOpenChange,
  onConfirm,
  entityLabel,
  requiredWord = "DELETE",
  matchCase = false,
  title,
  description,
  warningTitle = "Permanent Data Loss Warning",
  warningText,
  details,
  confirmLabel,
  inputId = "confirmation",
}: DeleteConfirmModalProps): React.ReactElement {
  const [loading, setLoading] = useState(false)
  const [confirmationText, setConfirmationText] = useState("")
  const [error, setError] = useState<string | null>(null)
  // The consequence lives in the warning banner, not in
  // <DialogDescription> — see the aria-describedby note on
  // <DialogContent> below.
  const warningId = React.useId()

  const isConfirmed = matchCase
    ? confirmationText === requiredWord
    : confirmationText.toLowerCase() === requiredWord.toLowerCase()

  const handleDelete = async () => {
    if (!isConfirmed) return
    setLoading(true)
    setError(null)
    try {
      await onConfirm()
      setConfirmationText("")
      onOpenChange(false)
    } catch (err) {
      console.error(`Failed to delete ${entityLabel.toLowerCase()}:`, err)
      setError(
        err instanceof Error
          ? err.message
          : `Failed to delete ${entityLabel.toLowerCase()}`,
      )
    } finally {
      setLoading(false)
    }
  }

  const handleCancel = () => {
    setConfirmationText("")
    setError(null)
    onOpenChange(false)
  }

  const resolvedTitle = title ?? `Delete ${entityLabel}`
  const resolvedDescription =
    description ??
    `This action cannot be undone. The ${entityLabel.toLowerCase()} will be permanently deleted.`
  const resolvedWarning =
    warningText ??
    `This ${entityLabel.toLowerCase()} and its associated data will be permanently removed. This action cannot be reversed.`
  const resolvedConfirmLabel = confirmLabel ?? `Delete ${entityLabel}`

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* aria-describedby is aimed at the WARNING BANNER, overriding
          Radix's default wiring to <DialogDescription>.
          The APG's point in giving a confirmation `role="alertdialog"`
          is that the accessible description carries the CONSEQUENCE.
          Here the description is the one-line summary while the banner
          is the specific blast radius the caller supplies (`warningText`
          — e.g. purge's "every message, task and action that referenced
          it is rewritten to a tombstone"). The banner is the sentence a
          screen-reader user needs before the confirm input, so it is the
          one aria-describedby names. The visible <DialogDescription> is
          still read in document order. */}
      <DialogContent
        alertDialog
        aria-describedby={warningId}
        className="w-[calc(100vw-2rem)] sm:!max-w-lg bg-card border-border text-card-foreground"
      >
        <DialogHeader>
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-destructive/15">
              <Trash2 className="h-4 w-4 text-destructive" />
            </div>
            <DialogTitle className="text-lg text-foreground">
              {resolvedTitle}
            </DialogTitle>
          </div>
          <DialogDescription className="text-muted-foreground">
            {resolvedDescription}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Warning Banner */}
          <div className="flex items-start gap-3 p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
            <AlertTriangle className="h-5 w-5 text-destructive mt-0.5 flex-shrink-0" />
            <div className="space-y-1" id={warningId}>
              <div className="text-sm font-medium text-destructive">
                {warningTitle}
              </div>
              <div className="text-xs text-destructive/80">{resolvedWarning}</div>
            </div>
          </div>

          {/* Entity-specific preview */}
          {details}

          {/* Confirmation Input */}
          <div className="space-y-2">
            <Label htmlFor={inputId} className="text-sm font-medium text-foreground">
              Type{" "}
              <span className="font-mono font-bold text-destructive">
                {requiredWord}
              </span>{" "}
              to confirm deletion
            </Label>
            <div className="relative">
              <Input
                id={inputId}
                value={confirmationText}
                onChange={(e) => setConfirmationText(e.target.value)}
                onKeyDown={onEnterSubmit(isConfirmed && !loading, handleDelete)}
                placeholder={`Type "${requiredWord}" to confirm`}
                className={cn(
                  "bg-background border-border text-foreground font-mono",
                  "focus:border-destructive focus:ring-destructive/20",
                  !isConfirmed &&
                    confirmationText.length > 0 &&
                    "border-destructive/50",
                )}
                disabled={loading}
              />
              <div className="absolute right-3 top-1/2 -translate-y-1/2">
                {isConfirmed ? (
                  <div className="h-2 w-2 rounded-full bg-destructive" />
                ) : (
                  <Lock className="h-3 w-3 text-muted-foreground" />
                )}
              </div>
            </div>
            {confirmationText.length > 0 && !isConfirmed && (
              <div className="text-xs text-destructive">
                Please type &quot;{requiredWord}&quot; exactly to confirm deletion
              </div>
            )}
          </div>

          {/* Error Message */}
          {error && (
            <div className="flex items-start gap-2 p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
              <AlertTriangle className="h-4 w-4 text-destructive mt-0.5 flex-shrink-0" />
              <div className="text-sm text-destructive">{error}</div>
            </div>
          )}
        </div>

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
            type="button"
            variant="destructive"
            onClick={handleDelete}
            size="sm"
            disabled={loading || !isConfirmed}
            className="bg-destructive hover:bg-destructive/90 text-destructive-foreground"
          >
            {loading ? (
              <>
                <div className="h-3 w-3 border-2 border-destructive-foreground border-t-transparent rounded-full animate-spin mr-2" />
                Deleting...
              </>
            ) : (
              <>
                <Trash2 className="h-3 w-3 mr-2" />
                {resolvedConfirmLabel}
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
