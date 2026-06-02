"use client"

import { useCallback, useState } from "react"

/**
 * Generic dialog state machine — **live-lookup** edition.
 *
 * Candidate D from the 2026-06-02 architecture review. Replaces the
 * snapshot-on-open shape introduced in Candidate F1 (2026-06-01),
 * which stored the row passed to `open(row)` inside `useState<T |
 * null>(null)`. Background refresh would update the underlying
 * zustand store, but the dialog kept rendering against the captured
 * snapshot. PR #74's Add-Note investigation traced the user-visible
 * "saved note disappears" symptom to exactly this: after the Edit
 * dialog saved a new note and the store updated, the View dialog
 * still rendered the pre-save object. A separate workaround had
 * already grown in tasks-dashboard.tsx — a `(stale)` SelectItem in
 * the assignee dropdown to handle the case where the snapshot's
 * `assigned_to` referred to an agent that had since been terminated
 * out of the live roster.
 *
 * This refactor retires the snapshot. The hook now stores a **key**
 * (typically `task.task_id`, `message.message_id`, etc.) and asks a
 * caller-supplied **selector** for the current row on every render.
 * When the selector is a zustand subscription, updates re-render the
 * dialog automatically; when it is a `useCallback` closure over
 * local component state, it re-runs when that state changes. Either
 * way, `data` is always the row as it exists right now — not as it
 * existed when the dialog opened.
 *
 * Usage with zustand source:
 *
 *     const viewDialog = useDialog<Task>(
 *       useCallback(
 *         (id) => useDataStore.getState().tasks.find(t => t.task_id === id) ?? null,
 *         [],
 *       ),
 *     )
 *     // …open passes the *key*, not the row.
 *     <button onClick={() => viewDialog.open(task.task_id)}>view</button>
 *     <ViewTaskDialog
 *       task={viewDialog.data}                          // always live
 *       open={viewDialog.isOpen}
 *       onOpenChange={(o) => { if (!o) viewDialog.close() }}
 *     />
 *
 * Usage with local component state:
 *
 *     const detailDialog = useDialog<Message>(
 *       useCallback(
 *         (id) => messages.find(m => m.message_id === id) ?? null,
 *         [messages],
 *       ),
 *     )
 *
 * Behaviour when the row is **deleted** out from under the dialog:
 * `data` becomes `null` while `isOpen` stays `true` (the user
 * explicitly opened the dialog). Consumers should treat
 * `isOpen && data === null` as the deleted-while-open signal — the
 * recommended response is to call `close()` and surface a toast.
 * See the tasks- / agents-dashboard auto-close effect for the
 * canonical pattern.
 */
export function useDialog<T, K = string>(
  selector: (key: K | null) => T | null,
): {
  /** True iff the dialog is currently open (i.e. a key is set). */
  readonly isOpen: boolean
  /**
   * The current row, looked up live via the selector. `null` if the
   * dialog is closed *or* the row has been deleted from the source
   * while the dialog was open.
   */
  readonly data: T | null
  /** Open the dialog for a specific key (the row's identity field). */
  open: (key: K) => void
  /** Close the dialog and clear the key. */
  close: () => void
} {
  const [key, setKey] = useState<K | null>(null)

  const open = useCallback((k: K) => setKey(k), [])
  const close = useCallback(() => setKey(null), [])

  // Live lookup — runs on every render, picks up store / state
  // updates immediately. If the row has been deleted from the
  // source, this is `null` while `isOpen` remains `true`.
  const data = selector(key)

  return {
    isOpen: key !== null,
    data,
    open,
    close,
  }
}
