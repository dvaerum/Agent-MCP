"use client"

import React from "react"
import {
  LayoutDashboard,
  Users,
  CheckSquare,
  Brain,
  BookOpen,
  MessageSquare,
  Settings
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { useSidebar as useSidebarUI } from "@/components/ui/sidebar"
import { useServerStore } from "@/lib/stores/server-store"
import { useDashboard, useSidebar } from "@/lib/store"

interface NavItem {
  title: string
  icon: React.ComponentType<{ className?: string }>
  view: 'overview' | 'agents' | 'tasks' | 'memories' | 'messages' | 'settings' | 'prompts'
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
    title: "Settings",
    icon: Settings,
    view: "settings",
    description: "Per-project worker-permission policy toggles"
  },
  {
    title: "Prompt Book",
    icon: BookOpen,
    view: "prompts",
    description: "Standardized prompts and workflows"
  }
]


export function Navigation() {
  const { currentView, setCurrentView } = useDashboard()
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
          setCurrentView(item.view)
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
              isActive={currentView === item.view}
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