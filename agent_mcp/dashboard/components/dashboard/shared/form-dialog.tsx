"use client"

import * as React from "react"
import { Loader2, type LucideIcon } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { onEnterSubmit } from "@/lib/keyboard"
import {
  useAsyncSubmit,
  type UseAsyncSubmitOptions,
} from "@/components/dashboard/shared/use-async-submit"

/**
 * Reusable create/edit dialog shell (Wave 5, CD-2).
 *
 * Owns the parts every form dialog repeats — the Radix `<Dialog>` +
 * `<DialogContent>` (with the mandatory `w-[calc(100vw-2rem)]` mobile
 * fallback and the viewport-height cap + scrollable body so a tall form
 * never overflows a phone), the title/description header, and the
 * Cancel/Submit footer wired to `useAsyncSubmit` (loading spinner,
 * success/error toast, close-on-success / stay-open-on-error).
 *
 * The caller supplies only the fields (`children`) and the `onSubmit`
 * mutation. Messages' compose is the first adopter; tasks + groups
 * (the next two Wave 5 resources) reuse it for their create/edit forms.
 *
 * a11y: `<DialogTitle>` is always rendered (Radix requires an
 * accessible name). `description` is optional but recommended — when
 * omitted, `aria-describedby` is explicitly cleared so Radix doesn't
 * warn about (and screen readers don't hunt for) a missing description.
 */
export interface FormDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Accessible name of the dialog. */
  title: React.ReactNode
  /** Accessible description — the one-line "what this form does". */
  description?: React.ReactNode
  /** Optional leading glyph in the title row. */
  icon?: LucideIcon
  /** The form fields. */
  children: React.ReactNode
  /** The mutation. Throw to keep the dialog open with an error toast. */
  onSubmit: () => Promise<void>
  /** Submit button label (idle). Default "Save". */
  submitLabel?: string
  /** Submit button label while in flight. Default "Saving…". */
  submittingLabel?: string
  /** Cancel button label. Default "Cancel". */
  cancelLabel?: string
  /** Disable submit (validation gate). Loading also disables it. */
  submitDisabled?: boolean
  /** Submit button variant. Default "default". */
  submitVariant?: React.ComponentProps<typeof Button>["variant"]
  /** Success toast copy (see `useAsyncSubmit`). */
  successMessage?: UseAsyncSubmitOptions<void>["successMessage"]
  /** Error toast fallback copy. */
  errorMessage?: string
  /** Called after a successful submit, before the auto-close. */
  onSuccess?: () => void
  /** Widen the dialog (sm:max-w-2xl) for multi-column forms. */
  wide?: boolean
  /** Extra footer content, rendered left of Cancel. */
  footerExtra?: React.ReactNode
  /**
   * Opt the underlying `<DialogContent>` into `role="alertdialog"` —
   * for a destructive/high-stakes confirm-style form (see
   * `components/ui/dialog.tsx`). Default false.
   */
  alertDialog?: boolean
  /**
   * Passed straight through to `useAsyncSubmit`: close the dialog on
   * a successful submit (default) or leave it open — for the rare
   * case where a successful submit reveals a result the operator
   * needs to see (e.g. a generated token) rather than dismissing.
   */
  closeOnSuccess?: boolean
}

export function FormDialog({
  open,
  onOpenChange,
  title,
  description,
  icon: Icon,
  children,
  onSubmit,
  submitLabel = "Save",
  submittingLabel = "Saving…",
  cancelLabel = "Cancel",
  submitDisabled = false,
  submitVariant = "default",
  successMessage,
  errorMessage,
  onSuccess,
  wide = false,
  footerExtra,
  alertDialog = false,
  closeOnSuccess = true,
}: FormDialogProps): React.ReactElement {
  const { submit, submitting } = useAsyncSubmit<void>({
    onSubmit,
    successMessage,
    errorMessage,
    onSuccess,
    onOpenChange,
    closeOnSuccess,
  })

  const canSubmit = !submitting && !submitDisabled

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Plain-string className (not cn()/template) so the mobile-width
          audit in test_dashboard_polish_mobile_pass.py can statically
          see the `w-[calc(100vw-2rem)]` fallback. The size class is a
          string concat for the same reason. flex-col + max-h caps the
          dialog to the viewport height; the body below scrolls. */}
      {/* className MUST start `{"w-[calc(100vw-2rem)]…"` (string literal
          adjacent to the brace) so the static mobile-width audit in
          test_dashboard_polish_mobile_pass.py can see the fallback; the
          size variant is a string concat for the same reason. */}
      <DialogContent
        {...(description ? {} : { "aria-describedby": undefined })}
        alertDialog={alertDialog}
        className={"w-[calc(100vw-2rem)] flex max-h-[calc(100dvh-2rem)] flex-col overflow-hidden [&>*]:min-w-0 " + (wide ? "sm:max-w-2xl" : "sm:max-w-lg")}
        onKeyDown={onEnterSubmit(canSubmit, () => void submit())}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {Icon && <Icon className="h-5 w-5 text-primary" />}
            {title}
          </DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>

        {/* Scrollable field area — header + footer stay pinned. */}
        <div className="flex-1 min-h-0 space-y-3 overflow-y-auto py-1 pr-1">
          {children}
        </div>

        <DialogFooter className="gap-2">
          {footerExtra}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            {cancelLabel}
          </Button>
          <Button
            type="button"
            variant={submitVariant}
            size="sm"
            onClick={() => void submit()}
            disabled={!canSubmit}
          >
            {submitting ? (
              <>
                <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                {submittingLabel}
              </>
            ) : (
              submitLabel
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
