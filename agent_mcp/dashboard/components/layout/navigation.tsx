"use client"

import React from "react"
import {
  LayoutDashboard,
  Users,
  CheckSquare,
  Brain,
  BookOpen,
  MessageSquare,
  CalendarClock,
  Settings,
  Network
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { useSidebar as useSidebarUI } from "@/components/ui/sidebar"
import { useServerStore } from "@/lib/stores/server-store"
import { useSidebar } from "@/lib/store"
import { useSectionRoute } from "@/lib/use-section-route"

interface NavItem {
  title: string
  icon: React.ComponentType<{ className?: string }>
  // Mirrors `DashboardSection` from lib/use-section-route.ts — kept
  // as a literal union here so a grep for the enum values lands on
  // this file too (the sidebar is where contributors reason about
  // "which menu items exist").
  view: 'overview' | 'agents' | 'tasks' | 'memories' | 'messages' | 'schedules' | 'settings' | 'system' | 'prompts'
  description?: string
  badge?: string
}

const navigationItems: NavItem[] = [
  {
    title: "Overview",
    icon: LayoutDashboard,
    view: "overview",
    description: "System overview and metrics"
  },
  {
    title: "Agents",
    icon: Users,
    view: "agents",
    description: "Manage and monitor agents"
  },
  {
    title: "Tasks", 
    icon: CheckSquare,
    view: "tasks",
    description: "Task orchestration and management"
  },
  {
    title: "Memories",
    icon: Brain,
    view: "memories",
    description: "Memory bank and context management"
  },
  {
    title: "Messages",
    icon: MessageSquare,
    view: "messages",
    description: "Inter-agent messaging — compose, read, history"
  },
  {
    title: "Schedules",
    icon: CalendarClock,
    view: "schedules",
    description: "Recurring scheduled directives — list, toggle, poke"
  },
  {
    title: "Settings",
    icon: Settings,
    view: "settings",
    description: "Per-project worker-permission policy toggles"
  },
  {
    title: "System",
    icon: Network,
    view: "system",
    description: "System architecture and real-time graph visualization"
  },
  {
    title: "Prompt Book",
    icon: BookOpen,
    view: "prompts",
    description: "Standardized prompts and workflows"
  }
]


export function Navigation() {
  // URL-driven section routing — clicking a nav item writes `?page=…`
  // into the URL so reload + share-links land back on the same
  // section. The hook keeps the legacy `useDashboard.currentView`
  // zustand slice in sync as a write-through so consumers like
  // <Header>'s page-title crumb keep working unchanged.
  const { currentSection, setSection } = useSectionRoute()
  const { isCollapsed } = useSidebar()
  // CC-25 (audit 2026-06-02): on mobile, the sidebar is rendered as
  // a Sheet over the content. Without this, clicking a nav item just
  // switches the view but leaves the Sheet open — user has to dismiss
  // it manually before they can see the page they navigated to.
  // setOpenMobile(false) closes the Sheet after a nav-click. Pulled
  // from the shadcn Sidebar primitive's useSidebar context (renamed
  // to useSidebarUI here to avoid the colliding zustand useSidebar
  // store hook above).
  const { isMobile, setOpenMobile } = useSidebarUI()

  const NavButton = ({ item, isActive = false }: { item: NavItem, isActive?: boolean }) => {
    const button = (
      <Button
        variant={isActive ? "secondary" : "ghost"}
        className={cn(
          "w-full justify-start gap-3 h-11",
          isCollapsed && "justify-center px-2",
          isActive && "bg-secondary text-secondary-foreground font-medium"
        )}
        onClick={() => {
          setSection(item.view)
          if (isMobile) setOpenMobile(false)
        }}
      >
        <item.icon className={cn("h-5 w-5", isActive && "text-primary")} />
        {!isCollapsed && (
          <>
            <span className="truncate">{item.title}</span>
            {item.badge && (
              <span className="ml-auto text-xs bg-primary text-primary-foreground px-1.5 py-0.5 rounded-full">
                {item.badge}
              </span>
            )}
          </>
        )}
      </Button>
    )

    if (isCollapsed && item.description) {
      return (
        <Tooltip>
          <TooltipTrigger asChild>
            {button}
          </TooltipTrigger>
          <TooltipContent side="right">
            <p className="font-medium">{item.title}</p>
            <p className="text-sm text-muted-foreground">{item.description}</p>
          </TooltipContent>
        </Tooltip>
      )
    }

    return button
  }

  return (
    <TooltipProvider>
      <nav className="space-y-2 p-3">
        {/* Primary Navigation */}
        <div className="space-y-1">
          {!isCollapsed && (
            <h3 className="px-3 py-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
              Dashboard
            </h3>
          )}
          {navigationItems.map((item) => (
            <NavButton
              key={item.view}
              item={item}
              isActive={currentSection === item.view}
            />
          ))}
        </div>


        {/* System Status Indicator (CC-11 audit 2026-06-02): now wired
            to the actual active-server status from useServerStore
            instead of being a hardcoded always-green pulsing dot that
            implied live status it didn't source. Dropped animate-pulse
            (CC-19) — constant motion in the chrome reads as busy. */}
        {!isCollapsed && <SystemStatusBadge />}
      </nav>
    </TooltipProvider>
  )
}

function SystemStatusBadge(): React.ReactElement {
  const { servers, activeServerId } = useServerStore()
  const active = servers.find((s) => s.id === activeServerId)
  const status = active?.status ?? "disconnected"
  // Three states: connected (emerald), connecting (amber), other
  // (muted). No pulsing — modern-minimal calls for static state.
  const tone =
    status === "connected"
      ? { dot: "bg-emerald-500", label: "Connected" }
      : status === "connecting"
        ? { dot: "bg-amber-500", label: "Connecting" }
        : { dot: "bg-muted-foreground/40", label: "Disconnected" }
  return (
    <div className="pt-4">
      <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-muted/50">
        <span
          aria-hidden
          className={cn("h-2 w-2 rounded-full", tone.dot)}
        />
        <span className="text-xs text-muted-foreground">{tone.label}</span>
      </div>
    </div>
  )
}