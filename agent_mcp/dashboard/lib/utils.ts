import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatTimestamp(timestamp: string | Date): string {
  if (!timestamp) return 'N/A'

  try {
    const date = new Date(timestamp)
    return date.toLocaleString()
  } catch {
    return String(timestamp)
  }
}

/**
 * arch-r5 #5 — the one `formatRelative`. Replaces 5 byte-drifted
 * copies (tasks-dashboard, tasks-mobile-list, agents-dashboard,
 * projects-overview-dashboard, agent-details-panel's
 * `formatTimestamp`) that disagreed on empty text, sub-minute
 * rendering ("just now" vs "Xs ago"), input shape (ISO string vs
 * epoch-seconds number), and >1-day tail (`Nd ago` vs
 * `toLocaleDateString()`).
 *
 * Canonical behavior (locked by arch-r5 #5): "just now" under 60s,
 * then `Nm/Nh/Nd ago`. A numeric `input` is treated as epoch
 * SECONDS (matches the projects-overview call site, the only
 * pre-existing numeric caller). Each call site preserves its own
 * empty-value text via `opts.emptyLabel`.
 */
export function formatRelative(
  input: string | number | Date | null | undefined,
  opts?: { emptyLabel?: string }
): string {
  const emptyLabel = opts?.emptyLabel ?? '—'
  if (input === null || input === undefined || input === '') return emptyLabel

  const ms = typeof input === 'number' ? input * 1000 : new Date(input).getTime()
  if (Number.isNaN(ms)) return typeof input === 'string' ? input : emptyLabel

  const diff = Date.now() - ms
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return `${Math.floor(diff / 86_400_000)}d ago`
}

export function debounce<T extends (...args: unknown[]) => unknown>(
  func: T,
  wait: number
): (...args: Parameters<T>) => void {
  let timeout: NodeJS.Timeout
  return (...args: Parameters<T>) => {
    clearTimeout(timeout)
    timeout = setTimeout(() => func(...args), wait)
  }
}

export function throttle<T extends (...args: unknown[]) => unknown>(
  func: T,
  limit: number
): (...args: Parameters<T>) => void {
  let inThrottle: boolean
  return (...args: Parameters<T>) => {
    if (!inThrottle) {
      func(...args)
      inThrottle = true
      setTimeout(() => inThrottle = false, limit)
    }
  }
}

export function generateId(): string {
  return Math.random().toString(36).substr(2, 9)
}

export function truncateText(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text
  return text.slice(0, maxLength - 3) + '...'
}

export function capitalizeFirst(str: string): string {
  return str.charAt(0).toUpperCase() + str.slice(1)
}

export function formatBytes(bytes: number, decimals = 2): string {
  if (bytes === 0) return '0 Bytes'
  
  const k = 1024
  const dm = decimals < 0 ? 0 : decimals
  const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB']
  
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i]
}

export function isValidUrl(string: string): boolean {
  try {
    new URL(string)
    return true
  } catch {
    return false
  }
}