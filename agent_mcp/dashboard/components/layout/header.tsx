"use client"

import { LogOut, Menu } from "lucide-react"
import { Button } from "@/components/ui/button"
import { ThemeToggle } from "./theme-toggle"
import { ProjectPicker } from "@/components/server/project-picker"
import { useSidebar as useSidebarUI } from "@/components/ui/sidebar"
import { useDashboard } from "@/lib/store"
import { loginUrl, logoutUrl } from "@/lib/urls"

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

// R12-F1: the dashboard had no logout UI anywhere, leaving an operator
// on a shared/kiosk browser no way to end their session for up to the
// cookie's 30-day expiry. The server route
// (POST /agent-mcp/logout — agent_mcp/router/login.py) was already
// correct (POST-only, CSRF-safe via SameSite cookie, httpOnly). This
// fires that POST then bounces to the login page — best-effort even if
// the request itself fails, since the goal is getting the operator off
// an authenticated screen.
async function handleLogout() {
  try {
    await fetch(logoutUrl(), { method: "POST", credentials: "include" })
  } finally {
    window.location.assign(loginUrl())
  }
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

        {/* Logout (R12-F1). Same hit-target/shrink conventions as the
            other header controls (see ThemeToggle). */}
        <Button
          variant="ghost"
          size="icon"
          onClick={handleLogout}
          className="shrink-0 h-10 w-10 sm:h-9 sm:w-9"
        >
          <LogOut className="h-4 w-4" />
          <span className="sr-only">Log out</span>
        </Button>
      </div>
    </header>
  )
}
