"use client"

import * as React from "react"
import { useState } from "react"
import { AlertTriangle, Trash2, type LucideIcon } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

/**
 * Tier 1 of the destructive-action confirmation model: a simple confirm
 * that NAMES its target. One click on the destructive button — no
 * type-to-confirm field.
 *
 * ## The tiers
 *
 * | Tier | Gate                                   | When |
 * |------|----------------------------------------|------|
 * | 0    | no dialog, success toast               | reversible in-product, no cascade, operator-recreatable |
 * | 1    | this modal — simple confirm, names the target | irreversible but recreatable, or bounded cascade, single scope |
 * | 2    | `<DeleteConfirmModal>`, type `DELETE`  | irreversible AND unrecoverable AND (unbounded cascade OR bulk) |
 * | 3    | `<DeleteConfirmModal requiredWord={name} matchCase>` | tier-2 conditions AND a named container whose destruction removes what is inside it, or visually confusable siblings |
 *
 * Escalation is **per-invocation, not per-entity**: the tier follows the
 * blast radius of THIS click. The same Delete button on the Tasks page
 * renders this modal for a leaf task and `<DeleteConfirmModal>` for one
 * with descendants. `remove-project-modal.tsx` was the first instance of
 * that idea (`destructiveReady = !deleteWorkspace || confirmName === projectName`
 * — the workspace checkbox is what escalates it); this pair generalises it.
 *
 * ## Why tier 1 is a real tier and not laziness
 *
 * Type-to-confirm is a scarce resource. Habituation to a repeated
 * warning begins at exposure #2 (Anderson et al., CHI 2015), and forcing
 * text-field interaction is the only intervention measured to RESIST
 * that habituation (Bravo-Lillo et al., SOUPS 2014). Spending it on
 * routine housekeeping — deleting a memory row, a schedule, a leaf task
 * — trains exactly the reflex that will one day carry an operator
 * straight through the Users / Groups dialogs, which cascade across
 * every project membership and capability grant. Keeping the cheap
 * actions cheap is what keeps the expensive gates expensive.
 *
 * ## The state machine
 *
 * `{busy, error}`. `onConfirm` rejecting keeps the dialog OPEN and shows
 * the message inline, so a failed delete is never mistaken for a
 * completed one; resolving closes it. Callers that also toast should
 * re-throw.
 */
export interface ConfirmActionModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Performs the action. Reject/throw to keep the dialog open. */
  onConfirm: () => Promise<void> | void
  /** Dialog heading, e.g. "Delete task". */
  title: string
  /**
   * The naming line. Tier 1's whole job is to say WHICH thing dies —
   * pass the title/key/id, not a generic "are you sure?".
   */
  description?: React.ReactNode
  /** Optional preview block (task id, memory value, …). */
  details?: React.ReactNode
  /** Destructive button label. Default "Delete". */
  confirmLabel?: string
  /** Destructive button label while in flight. Default "Deleting…". */
  busyLabel?: string
  /** `data-testid` on the destructive button, for page-level tests. */
  confirmTestId?: string
  /**
   * Hold the confirm button disarmed while the caller is still deciding
   * (e.g. the Tasks dialog waiting on its blast-radius preview — it must
   * not offer a one-click delete before it knows the task is a leaf).
   */
  confirmDisabled?: boolean
  /**
   * Icon for the header badge + confirm button. Defaults to a trash can;
   * override for actions that are destructive but NOT deletions (e.g.
   * Terminate, which is a soft-delete — a trash can would over-state it).
   */
  icon?: LucideIcon
}

export function ConfirmActionModal({
  open,
  onOpenChange,
  onConfirm,
  title,
  description,
  details,
  confirmLabel = "Delete",
  busyLabel = "Deleting…",
  confirmTestId,
  confirmDisabled = false,
  icon: Icon = Trash2,
}: ConfirmActionModalProps): React.ReactElement {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A reopened dialog must not show the previous attempt's failure.
  React.useEffect(() => {
    if (open) setError(null)
  }, [open])

  const handleConfirm = async () => {
    setBusy(true)
    setError(null)
    try {
      await onConfirm()
      onOpenChange(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* `alertDialog` — every destructive confirm announces as
          role="alertdialog" (W3C ARIA APG; see the prop's note in
          components/ui/dialog.tsx). Tier 1 needs it just as much as
          tier 2: it is still a confirmation prompt.

          Unlike <DeleteConfirmModal>, the consequence here lives in
          <DialogDescription> — that IS the naming line ("Delete task
          'X'? This cannot be undone.") — so Radix's default
          aria-describedby wiring already points at the right sentence
          and no override is needed.

          `w-[calc(100vw-2rem)]` is the mobile-width fallback pinned by
          tests/test_dashboard_polish_mobile_pass.py (CC-14); the body
          scrolls rather than the viewport so a long details slot stays
          usable at 390x844. Keeping the className on <DialogContent>
          (rather than a bare tag) is also what makes this dialog
          VISIBLE to that audit — the schedules confirm this replaced
          had no className at all and was silently exempt. */}
      <DialogContent
        alertDialog
        className="w-[calc(100vw-2rem)] sm:!max-w-md bg-card border-border text-card-foreground max-h-[90dvh] overflow-y-auto"
      >
        <DialogHeader>
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-destructive/15 flex-shrink-0">
              <Icon className="h-4 w-4 text-destructive" />
            </div>
            <DialogTitle className="text-lg text-foreground">{title}</DialogTitle>
          </div>
          {description && (
            <DialogDescription className="text-muted-foreground">
              {description}
            </DialogDescription>
          )}
        </DialogHeader>

        {(details || error) && (
          <div className="space-y-3">
            {details}
            {error && (
              <div className="flex items-start gap-2 p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
                <AlertTriangle className="h-4 w-4 text-destructive mt-0.5 flex-shrink-0" />
                <div className="text-sm text-destructive break-words">
                  {error}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Cancel-left / destructive-right (shadcn footer idiom, pinned
            for the Tasks page by test_dashboard_tasks_popup_polish.py). */}
        <DialogFooter className="gap-2 pt-4">
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
            variant="destructive"
            size="sm"
            onClick={() => void handleConfirm()}
            disabled={busy || confirmDisabled}
            data-testid={confirmTestId}
          >
            {busy ? (
              busyLabel
            ) : (
              <>
                <Icon className="h-3.5 w-3.5 mr-1.5" />
                {confirmLabel}
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
