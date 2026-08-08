"use client"

/**
 * Minimal in-app toast primitive.
 *
 * Bug surfaced by Firefox-MCP click-through on 2026-06-17 against
 * v5.0.47 (commit ``0ea1858``): the Agents page Deploy modal
 * accepted an invalid agent_id, the server returned 400 with a clear
 * ``{message: ...}`` body, but the client only logged to console and
 * silently closed the dialog. The user saw nothing.
 *
 * This module provides:
 *
 *   * A tiny zustand store (zustand is already a dashboard dep — no
 *     new packages, no Nix npmDepsHash bump). Pre-PR the project
 *     shipped no toast library at all; adding `sonner` would have
 *     required regenerating two `npmDepsHash` values in
 *     ``nix/package.nix`` / ``nix/packages.nix``.
 *
 *   * A ``<Toaster />`` portal mounted in ``app/layout.tsx`` that
 *     renders the pending toast list at the top-right of the
 *     viewport. Toasts auto-dismiss after ``DEFAULT_DURATION_MS``
 *     (longer for errors so the user has time to read the message);
 *     a close button is always present.
 *
 *   * A ``toast(opts)`` imperative API plus typed convenience helpers
 *     ``toastError(err)`` / ``toastSuccess(message)`` so callers
 *     don't need to know the store internals. ``toastError``
 *     unwraps ``ApiError`` from ``@/lib/api`` and prefers its
 *     ``.message`` (which already prefers the server's body
 *     ``{message: ...}`` field), so showing the server's exact
 *     validation text is a one-liner at every mutation site.
 *
 * The visual style intentionally matches the rest of the dashboard
 * (shadcn-ish: ``bg-card border-border text-card-foreground`` with a
 * tinted accent border per variant). No animation library — the
 * fade/slide uses Tailwind's transition utilities.
 */

import { useEffect, useState } from 'react'
import { create } from 'zustand'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'

import { ApiError } from '@/lib/api'
import { cn } from '@/lib/utils'

// ---------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------

export type ToastVariant = 'error' | 'success' | 'info'

/**
 * A single follow-up control rendered inside the toast — in practice
 * "Undo".
 *
 * M3's snackbar guidance is explicit that this is what the slot is
 * for: *"To allow users to amend choices, display an 'Undo' action."*
 * It is also the enabling half of the older Material confirmation
 * rule — *"confirmation isn't necessary when the consequences of an
 * action are reversible"* — which a dashboard can only lean on once a
 * reversible action can actually OFFER the reversal.
 *
 * ``onAction`` may be async. The toast awaits it, dismisses itself on
 * success, and — critically — surfaces an ERROR toast if it rejects.
 * A silent failure here would be the worst possible outcome: the user
 * clicked Undo, saw the toast vanish, and believes the state was
 * restored when it wasn't.
 */
export interface ToastAction {
  label: string
  onAction: () => void | Promise<void>
}

export interface ToastOptions {
  title?: string
  description: string
  variant?: ToastVariant
  durationMs?: number
  action?: ToastAction
}

interface ToastItem
  extends Required<Omit<ToastOptions, 'title' | 'action'>> {
  id: number
  title?: string
  action?: ToastAction
}

interface ToastStore {
  toasts: ToastItem[]
  push: (opts: ToastOptions) => number
  dismiss: (id: number) => void
  reset: () => void
}

export const DEFAULT_TOAST_DURATION_MS = 4500
// Errors stay up longer — the user is more likely to need to read
// (and possibly act on) a server message than a "saved" confirmation.
export const DEFAULT_ERROR_TOAST_DURATION_MS = 8000
// An action toast is a DEADLINE, not just a notification: once it
// auto-dismisses the undo is gone. M3 puts the ceiling for an
// actionable snackbar at ~10s, which is also long enough to notice,
// read and reach the button on a phone.
export const ACTION_TOAST_DURATION_MS = 10000

let nextId = 1

const useToastStore = create<ToastStore>((set) => ({
  toasts: [],
  push: (opts) => {
    const variant: ToastVariant = opts.variant ?? 'info'
    const id = nextId++
    const item: ToastItem = {
      id,
      title: opts.title,
      description: opts.description,
      variant,
      action: opts.action,
      durationMs:
        opts.durationMs ??
        (opts.action
          ? ACTION_TOAST_DURATION_MS
          : variant === 'error'
            ? DEFAULT_ERROR_TOAST_DURATION_MS
            : DEFAULT_TOAST_DURATION_MS),
    }
    set((s) => ({ toasts: [...s.toasts, item] }))
    return id
  },
  dismiss: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  reset: () => set({ toasts: [] }),
}))

/** Test-only: drop every pending toast between cases. */
export function __resetToastsForTests(): void {
  useToastStore.getState().reset()
}

// ---------------------------------------------------------------------
// Imperative API (callable from anywhere, including non-React code)
// ---------------------------------------------------------------------

export function toast(opts: ToastOptions): number {
  return useToastStore.getState().push(opts)
}

export function toastSuccess(description: string, title?: string): number {
  return toast({ variant: 'success', description, title })
}

/**
 * Surface a caught error to the user.
 *
 * Prefers ``ApiError.message`` (which itself prefers the server's
 * JSON ``{message: ...}`` payload — see ``ApiClient.request`` in
 * lib/api.ts), falling back to ``Error.message`` and finally to a
 * generic "Request failed" so the toast is never empty.
 *
 * ``fallback`` lets the caller hint what was being attempted (e.g.
 * "Failed to create agent") — it's used as the toast TITLE while the
 * server message goes in the description. If ``fallback`` is omitted
 * and we have a server message, the message becomes the description
 * with no title.
 */
export function toastError(err: unknown, fallback?: string): number {
  let description = 'Request failed.'
  if (err instanceof ApiError) {
    description = err.message || description
  } else if (err instanceof Error) {
    description = err.message || description
  } else if (typeof err === 'string') {
    description = err
  }
  return toast({
    variant: 'error',
    title: fallback,
    description,
  })
}

/**
 * Success toast for a reversible mutation, carrying an honest Undo.
 *
 * ``undo`` should issue the exact inverse call and refresh whatever
 * the caller refreshed after the original mutation. Only wire this up
 * where the inverse genuinely restores the prior state — an "Undo"
 * that quietly produces something *different* is worse than no undo
 * at all, because the user stops checking.
 *
 * On success the restore is confirmed with its own toast
 * (``undoneMessage``); on failure ``<ToastView>`` surfaces the error,
 * so the user is never left believing a failed undo worked.
 */
export function toastUndo(
  description: string,
  undo: () => Promise<void>,
  opts?: { title?: string; undoneMessage?: string; label?: string },
): number {
  return toast({
    variant: 'success',
    title: opts?.title,
    description,
    action: {
      label: opts?.label ?? 'Undo',
      onAction: async () => {
        await undo()
        toastSuccess(opts?.undoneMessage ?? 'Restored.')
      },
    },
  })
}

// ---------------------------------------------------------------------
// Toaster portal
// ---------------------------------------------------------------------

const VARIANT_CLASSES: Record<ToastVariant, string> = {
  error: 'border-destructive/60 text-destructive-foreground',
  success: 'border-emerald-500/60',
  info: 'border-primary/40',
}

const VARIANT_ICONS: Record<ToastVariant, React.ReactNode> = {
  error: <AlertCircle className="h-4 w-4 text-destructive" />,
  success: <CheckCircle2 className="h-4 w-4 text-emerald-500" />,
  info: <Info className="h-4 w-4 text-primary" />,
}

function ToastView({ item }: { item: ToastItem }) {
  const dismiss = useToastStore((s) => s.dismiss)
  const [running, setRunning] = useState(false)

  useEffect(() => {
    // Don't yank the toast out from under an in-flight action.
    if (running) return
    const t = setTimeout(() => dismiss(item.id), item.durationMs)
    return () => clearTimeout(t)
  }, [item.id, item.durationMs, dismiss, running])

  const action = item.action
  const handleAction = async () => {
    if (!action || running) return
    setRunning(true)
    try {
      await action.onAction()
      dismiss(item.id)
    } catch (e) {
      // Honesty rule: a failed Undo must never look like a successful
      // one. Replace the toast with the error rather than silently
      // dropping it.
      dismiss(item.id)
      toastError(e, `${action.label} failed`)
    } finally {
      setRunning(false)
    }
  }

  return (
    <div
      role={item.variant === 'error' ? 'alert' : 'status'}
      aria-live={item.variant === 'error' ? 'assertive' : 'polite'}
      data-testid="toast"
      data-variant={item.variant}
      className={cn(
        'pointer-events-auto flex w-full items-start gap-3 rounded-lg',
        'border bg-card text-card-foreground shadow-lg shadow-black/30',
        'px-4 py-3 transition-all duration-200',
        VARIANT_CLASSES[item.variant],
      )}
    >
      <div className="mt-0.5 flex-shrink-0">{VARIANT_ICONS[item.variant]}</div>
      {/* The action lives INSIDE this min-w-0 column, on its own row —
          not as a fourth inline cell next to icon/text/close. At 390px
          the toast is only ~358px wide; an inline action would compete
          with the description for that width and push the close button
          off-canvas the moment either string got long. Stacking costs
          one row of height and cannot overflow. */}
      <div className="min-w-0 flex-1" data-testid="toast-body">
        {item.title ? (
          <div className="text-sm font-semibold leading-tight">
            {item.title}
          </div>
        ) : null}
        <div
          className={cn(
            'text-sm text-card-foreground/90 break-words',
            item.title ? 'mt-1' : '',
          )}
        >
          {item.description}
        </div>
        {action ? (
          <div className="mt-2 flex justify-end">
            <button
              type="button"
              data-testid="toast-action"
              onClick={() => void handleAction()}
              disabled={running}
              className={cn(
                'rounded-md border border-border px-2.5 py-1',
                'text-xs font-semibold uppercase tracking-wide',
                'text-primary hover:bg-muted/60 transition-colors',
                'focus-visible:outline-none focus-visible:ring-2',
                'focus-visible:ring-ring disabled:opacity-60',
              )}
            >
              {running ? 'Working…' : action.label}
            </button>
          </div>
        ) : null}
      </div>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => dismiss(item.id)}
        className="-mr-1 -mt-1 rounded p-1 text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  )
}

export function Toaster() {
  const toasts = useToastStore((s) => s.toasts)

  // The live region is ALWAYS mounted, even with nothing to show.
  //
  // Two reasons, both load-bearing:
  //
  //   1. Radix's modal Dialog runs `hideOthers(content)` on mount,
  //      stamping `aria-hidden="true"` on every body child that exists
  //      at that moment. The `aria-hidden` package deliberately spares
  //      `[aria-live]` nodes — but only ones it can see when it runs.
  //      Returning `null` while idle made that a coin flip: a toast
  //      raised from inside an open dialog (project memberships, for
  //      one) could land inside an aria-hidden subtree and never be
  //      announced. An Undo a screen reader can't reach isn't an undo.
  //
  //   2. It is the correct live-region idiom regardless: assistive
  //      tech observes a region that already exists, rather than
  //      discovering one that materialises with its first message.
  //
  // The idle region is an empty `pointer-events-none` box — it renders
  // nothing and intercepts nothing.
  return (
    <div
      aria-live="polite"
      aria-label="Notifications"
      data-testid="toaster"
      className={cn(
        'pointer-events-none fixed top-4 right-4 z-[100]',
        'flex w-[calc(100vw-2rem)] max-w-sm flex-col gap-2',
      )}
    >
      {toasts.map((t) => (
        <ToastView key={t.id} item={t} />
      ))}
    </div>
  )
}
