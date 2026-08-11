import { vi } from "vitest"

/**
 * Stub `window.matchMedia` for jsdom tests that drive a component off
 * `useMediaQuery` / `useIsMobile` (jsdom ships no matchMedia).
 *
 * `mobile` sets the match result every query resolves to, so a test can
 * pin the viewport: `setMatchMedia(true)` renders the mobile tree,
 * `setMatchMedia(false)` the desktop tree. Returns the change-listener
 * set so a test can simulate a live viewport flip if it needs one.
 */
export function setMatchMedia(mobile: boolean): { flip: (next: boolean) => void } {
  let matches = mobile
  const listeners = new Set<() => void>()
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    get matches() {
      return matches
    },
    media: query,
    onchange: null,
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    addListener: (cb: () => void) => listeners.add(cb),
    removeListener: (cb: () => void) => listeners.delete(cb),
    dispatchEvent: () => true,
  })) as unknown as typeof window.matchMedia
  return {
    flip(next: boolean) {
      matches = next
      listeners.forEach((cb) => cb())
    },
  }
}
