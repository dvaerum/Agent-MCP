"use client"

import { useCallback, useState } from "react"

/**
 * Generic dialog state machine.
 *
 * Before this hook every dashboard dialog repeated the same ~3-line
 * pattern:
 *
 *     const [open, setOpen] = useState<boolean>(false)
 *     const [data, setData] = useState<T | null>(null)
 *     // ...later
 *     setOpen(true); setData(row)
 *     // ...on close
 *     setOpen(false); setData(null)
 *
 * The boolean and the nullable row are redundant — `data !== null`
 * already encodes "open". This hook collapses the pair into a single
 * piece of state and exposes a small, named API.
 *
 * Usage:
 *
 *     const view = useDialog<Task>()
 *     <button onClick={() => view.open(task)}>view</button>
 *     <ViewTaskDialog
 *       open={view.isOpen}
 *       task={view.data}
 *       onOpenChange={(o) => { if (!o) view.close() }}
 *     />
 *
 * Candidate F1 from the 2026-06-01 architecture review.
 */
export function useDialog<T>(): {
  /** True iff a row is currently being viewed / edited / deleted. */
  readonly isOpen: boolean
  /** The row the dialog was opened for, or `null` when closed. */
  readonly data: T | null
  /** Open the dialog for a specific row. */
  open: (row: T) => void
  /** Close the dialog and clear the row. */
  close: () => void
} {
  const [data, setData] = useState<T | null>(null)

  const open = useCallback((row: T) => setData(row), [])
  const close = useCallback(() => setData(null), [])

  return {
    isOpen: data !== null,
    data,
    open,
    close,
  }
}
