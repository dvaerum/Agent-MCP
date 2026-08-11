import { useMediaQuery } from "@/hooks/use-media-query"

// 768px = Tailwind `md`. Kept as the sidebar's mobile boundary (its
// original value). NOTE: this is a DIFFERENT boundary from the shared
// data-table's, which splits at Tailwind `sm` (640px) to match its own
// `hidden sm:block` CSS — see <ResponsiveDataTable>. Don't fold the two
// together; they are deliberately different breakpoints.
const MOBILE_BREAKPOINT = 768

export function useIsMobile() {
  return useMediaQuery(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`)
}
