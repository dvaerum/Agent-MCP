"use client"

/**
 * URL <-> active-section bridging hook.
 *
 * The dashboard's "current section" (Overview / Agents / Tasks /
 * Memories / Messages / Settings / Prompt Book) used to live only in
 * zustand state (`useDashboard.currentView`). That meant:
 *   - reload always reset to Overview, and
 *   - share-links could not deep-link into a specific section.
 *
 * This hook makes the URL the source of truth via `?page=<section>`,
 * e.g. `/agent-mcp/app/<project>/?page=tasks`.
 *
 * Why query-param vs proper route segments. The dashboard is a single
 * client page that swaps a `currentView` enum — route segments would
 * require restructuring app/ into `app/[section]/page.tsx` files and
 * coordinating with the path-prefix adapter (PR #56) that mounts the
 * dashboard at `/agent-mcp/app/<project>/`. Query-param is a
 * one-file change and works with the existing mount unchanged.
 *
 * Why a dedicated hook vs inlining useSearchParams in page.tsx. So
 * the sidebar Navigation component and page.tsx both go through the
 * same code path — same validation, same default fallback, same
 * setter semantics. The hook is the single source of truth for the
 * URL contract.
 *
 * Fallback. Missing `?page=` or unknown values fall back to
 * 'overview' so the bare dashboard URL keeps working.
 */

import { useCallback, useEffect, useMemo } from "react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { useDashboard } from "@/lib/store"

export type DashboardSection =
  | "overview"
  | "agents"
  | "tasks"
  | "memories"
  | "messages"
  | "schedules"
  | "settings"
  | "system"
  | "prompts"

const KNOWN_SECTIONS: ReadonlySet<DashboardSection> = new Set<DashboardSection>(
  ["overview", "agents", "tasks", "memories", "messages", "schedules", "settings", "system", "prompts"],
)

const DEFAULT_SECTION: DashboardSection = "overview"
const PAGE_PARAM = "page"

function parseSection(raw: string | null): DashboardSection {
  if (raw && (KNOWN_SECTIONS as Set<string>).has(raw)) {
    return raw as DashboardSection
  }
  return DEFAULT_SECTION
}

export interface UseSectionRouteResult {
  /** Current section derived from `?page=` (defaults to 'overview'). */
  currentSection: DashboardSection
  /**
   * Navigate to a section. Writes the new value into `?page=` on the
   * current pathname via router.replace (no history entry per click;
   * see note below).
   *
   * Uses router.replace (not push) so the back-button still returns
   * to wherever the user came from BEFORE entering the dashboard
   * (e.g. the project picker) instead of cycling through every
   * sidebar click. Browser back/forward still navigate between
   * sections that were entered separately (e.g. via deep-link).
   */
  setSection: (section: DashboardSection) => void
}

export function useSectionRoute(): UseSectionRouteResult {
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const setCurrentView = useDashboard((s) => s.setCurrentView)

  const currentSection = useMemo(
    () => parseSection(searchParams?.get(PAGE_PARAM) ?? null),
    [searchParams],
  )

  // Keep the zustand `currentView` slice in sync with the URL so
  // existing consumers (e.g. <Header> page-title crumb) keep working
  // unchanged. The hook treats the URL as the source of truth; this
  // effect is the write-through that propagates URL changes (initial
  // load, back/forward, deep-link) into the legacy store.
  useEffect(() => {
    setCurrentView(currentSection)
  }, [currentSection, setCurrentView])

  const setSection = useCallback(
    (section: DashboardSection) => {
      // No-op if already on this section — avoids a redundant
      // router.replace that would re-trigger render+effect chains.
      if (section === currentSection) return

      const params = new URLSearchParams(
        searchParams ? searchParams.toString() : "",
      )
      if (section === DEFAULT_SECTION) {
        // Keep the bare URL clean for the default — omit the param
        // when it would just be `?page=overview`.
        params.delete(PAGE_PARAM)
      } else {
        params.set(PAGE_PARAM, section)
      }
      const query = params.toString()
      const target = query ? `${pathname}?${query}` : pathname
      router.replace(target)
    },
    [router, pathname, searchParams, currentSection],
  )

  return { currentSection, setSection }
}
