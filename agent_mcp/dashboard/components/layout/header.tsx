"use client"

import { Menu } from "lucide-react"
import { Button } from "@/components/ui/button"
import { ThemeToggle } from "./theme-toggle"
import { ProjectPicker } from "@/components/server/project-picker"
import { useSidebar as useSidebarUI } from "@/components/ui/sidebar"
import { useDashboard } from "@/lib/store"

// CC-9 (audit 2026-06-02): page-title map for the header crumb. On
// mobile the sidebar collapses to a Sheet that, once closed, leaves
// the user with no indication of which page they're on. We surface
// the current view name in the header instead. Desktop hides this
// since the active sidebar item already conveys the same.
const VIEW_TITLES: Record<string, string> = {
  overview: "Overview",
  agents: "Agents",
  tasks: "Tasks",
  memories: "Memories",
  messages: "Messages",
  settings: "Settings",
  prompts: "Prompt Book",
  system: "System",
}

export function Header() {
  const { toggleSidebar } = useSidebarUI()
  const { currentView } = useDashboard()
  const pageTitle = VIEW_TITLES[currentView] ?? "Dashboard"

  return (
    <header className="sticky top-0 z-50 w-full border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex h-16 items-center gap-3 sm:gap-4 px-4 sm:px-6">
        {/* Menu Toggle Button. Bumped to h-10 w-10 on mobile
            (CC-12 audit 2026-06-02) so the hit target clears the 40px
            floor; desktop keeps the shadcn default 36px. */}
        <Button
          variant="ghost"
          size="icon"
          onClick={toggleSidebar}
          className="shrink-0 lg:hidden h-10 w-10 sm:h-9 sm:w-9"
        >
          <Menu className="h-5 w-5" />
          <span className="sr-only">Toggle navigation menu</span>
        </Button>

        {/* Current-page crumb (CC-9). Hidden on lg+ where the sidebar's
            active item already conveys the same info. */}
        <h2
          aria-live="polite"
          className="lg:hidden text-base sm:text-lg font-semibold tracking-tight text-foreground truncate"
        >
          {pageTitle}
        </h2>

        {/* Project Picker */}
        <div className="flex-1 min-w-0">
          <ProjectPicker />
        </div>

        {/* Theme Toggle */}
        <ThemeToggle />
      </div>
    </header>
  )
}
