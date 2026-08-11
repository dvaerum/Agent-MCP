"use client"

import { useCallback, useRef, useState } from "react"
import { toastError, toastSuccess } from "@/components/ui/toast"

/**
 * The submit → loading → toast → close/stay-open state machine that
 * every create/edit form in the dashboard hand-rolled (register-agent,
 * edit-agent, create-memory, delete-confirm, messages compose all had
 * their own copy). Wave 5 (CD-2): one hook owns it so the extracted
 * resource forms — and the shared `<FormDialog>` built on top of it —
 * behave identically.
 *
 * Contract:
 *   - `submit()` runs `onSubmit`, guarding against a concurrent
 *     double-fire (the classic double-click-sends-twice bug — guarded
 *     with a ref so a second call in the same tick is dropped even
 *     before the `submitting` state re-renders).
 *   - On success: fire `successMessage` toast (if any), call
 *     `onSuccess`, then — unless `closeOnSuccess` is false — close via
 *     `onOpenChange(false)`. The register-agent flow that swaps to a
 *     result pane instead of closing sets `closeOnSuccess: false`.
 *   - On failure: keep the dialog OPEN (never call `onOpenChange`),
 *     surface `errorMessage` via `toastError`, expose the message on
 *     `error` for an inline banner, and call `onError`. This is the
 *     "stay open with the operator's input intact" property the
 *     extracted dialogs already promised individually.
 */
export interface UseAsyncSubmitOptions<T> {
  /** The async mutation. Its resolved value flows to `onSuccess` /
   * `successMessage`; throw to trigger the error path. */
  onSubmit: () => Promise<T>
  /** Success toast — a string, or a function of the result. Omit for
   * no toast (the caller toasts itself, or none is wanted). */
  successMessage?: string | ((result: T) => string)
  /** Fallback passed to `toastError` on failure. Omit for no toast. */
  errorMessage?: string
  /** Ran after a successful submit, before the auto-close. */
  onSuccess?: (result: T) => void
  /** Ran on failure, after the error toast. */
  onError?: (error: unknown) => void
  /** The dialog's open setter — called with `false` to close on
   * success. Omit for a non-dialog form. */
  onOpenChange?: (open: boolean) => void
  /** Auto-close on success via `onOpenChange(false)`. Default `true`. */
  closeOnSuccess?: boolean
}

export interface AsyncSubmit {
  /** Run the mutation. Safe to call from an onClick or Enter handler;
   * concurrent calls are dropped. */
  submit: () => Promise<void>
  /** True while the mutation is in flight. */
  submitting: boolean
  /** Message from the last failed submit, or null. For an inline
   * banner; the error toast fires independently. */
  error: string | null
  /** Clear the inline error (e.g. when the operator edits a field). */
  clearError: () => void
}

export function useAsyncSubmit<T = unknown>(
  options: UseAsyncSubmitOptions<T>,
): AsyncSubmit {
  const {
    onSubmit,
    successMessage,
    errorMessage,
    onSuccess,
    onError,
    onOpenChange,
    closeOnSuccess = true,
  } = options

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Ref guard, not the `submitting` state: a second call in the SAME
  // tick (double-click) still sees the pre-render `submitting === false`
  // through its closure and would fire twice. The ref flips synchronously.
  const inFlight = useRef(false)

  const submit = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    setSubmitting(true)
    setError(null)
    try {
      const result = await onSubmit()
      if (successMessage !== undefined) {
        toastSuccess(
          typeof successMessage === "function"
            ? successMessage(result)
            : successMessage,
        )
      }
      onSuccess?.(result)
      if (closeOnSuccess) onOpenChange?.(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      if (errorMessage !== undefined) toastError(err, errorMessage)
      onError?.(err)
      // Deliberately NOT closing — stay open so the operator's input is
      // intact and the inline error is visible.
    } finally {
      inFlight.current = false
      setSubmitting(false)
    }
  }, [
    onSubmit,
    successMessage,
    errorMessage,
    onSuccess,
    onError,
    onOpenChange,
    closeOnSuccess,
  ])

  const clearError = useCallback(() => setError(null), [])

  return { submit, submitting, error, clearError }
}
