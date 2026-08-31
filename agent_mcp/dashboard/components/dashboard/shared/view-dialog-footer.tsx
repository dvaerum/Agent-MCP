"use client"

import * as React from "react"
import { DialogFooter } from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

/**
 * Footer for a read-only View/Detail dialog (Edit / Delete / secondary
 * actions + Close) — the shape duplicated four times
 * (`view-task-dialog.tsx`, `agent-detail-dialog.tsx`,
 * `view-message-modal.tsx` on `<DialogFooter>`; `view-memory-modal.tsx`
 * hand-rolled) with no shared home, same gap as `FormDialog`/
 * `FilterField` before their own extraction.
 *
 * `<DialogFooter>`'s own default (`flex-col-reverse` below `sm:`) is
 * deliberate for CONFIRM dialogs: full-width stacked buttons are a
 * common mobile safety pattern for Cancel-vs-Delete, and it stays
 * untouched there. A view dialog has no such destructive-confirm
 * concern and can carry 3-5 buttons (agent-detail-dialog's Send
 * directive / Edit / Terminate / Purge / Close) — stacking every one
 * full-width ate most of the dialog's vertical space on a phone (the
 * reported bug). This wrapper overrides just the mobile half: buttons
 * wrap onto as many rows as needed, sized to their own content instead
 * of the viewport width, so 2-3 fit per row instead of one giant
 * full-width button per row.
 */
export function ViewDialogFooter({
  className,
  ...props
}: React.ComponentProps<typeof DialogFooter>): React.ReactElement {
  return (
    <DialogFooter
      className={cn("flex-row flex-wrap justify-end gap-2", className)}
      {...props}
    />
  )
}
