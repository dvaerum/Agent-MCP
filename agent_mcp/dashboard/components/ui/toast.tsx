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

import { useEffect } from 'react'
import { create } from 'zustand'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'

import { ApiError } from '@/lib/api'
import { cn } from '@/lib/utils'

// ---------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------

export type ToastVariant = 'error' | 'success' | 'info'

export interface ToastOptions {
  title?: string
  description: string
  variant?: ToastVariant
  durationMs?: number
}

interface ToastItem extends Required<Omit<ToastOptions, 'title'>> {
  id: number
  title?: string
}

interface ToastStore {
  toasts: ToastItem[]
  push: (opts: ToastOptions) => number
  dismiss: (id: number) => void
}

const DEFAULT_DURATION_MS = 4500
// Errors stay up longer — the user is more likely to need to read
// (and possibly act on) a server message than a "saved" confirmation.
const DEFAULT_ERROR_DURATION_MS = 8000

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
      durationMs:
        opts.durationMs ??
        (variant === 'error'
          ? DEFAULT_ERROR_DURATION_MS
          : DEFAULT_DURATION_MS),
    }
    set((s) => ({ toasts: [...s.toasts, item] }))
    return id
  },
  dismiss: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

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

  useEffect(() => {
    const t = setTimeout(() => dismiss(item.id), item.durationMs)
    return () => clearTimeout(t)
  }, [item.id, item.durationMs, dismiss])

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
      <div className="min-w-0 flex-1">
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

  if (toasts.length === 0) {
    return null
  }

  return (
    <div
      aria-live="polite"
      aria-label="Notifications"
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
