import * as React from "react"

/**
 * Subscribe to a CSS media query and get its current match as state.
 *
 * The primitive behind `useIsMobile` (below) and the shared
 * <ResponsiveDataTable> single-breakpoint render. matchMedia is the
 * right tool — it fires on the exact viewport boundary the CSS uses,
 * with no resize-listener debounce guesswork.
 *
 * SSR / first-paint contract: returns `false` on the very first render
 * (before the mount effect runs) so server prerender and the client's
 * first render agree — no hydration mismatch. The effect then syncs to
 * the real viewport. Consumers that render two trees off this value
 * must therefore treat `false` as their consistent default (the table
 * defaults to the desktop tree, then swaps if the effect reports
 * mobile). Guarded against a jsdom/Node environment where
 * `window.matchMedia` is absent — it simply stays `false` there.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = React.useState(false)

  React.useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange()
    mql.addEventListener("change", onChange)
    return () => mql.removeEventListener("change", onChange)
  }, [query])

  return matches
}
