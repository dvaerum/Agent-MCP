"use client"

import * as React from "react"
import { PanelLeftClose, PanelLeftOpen, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarRail,
  useSidebar as useSidebarUI
} from "@/components/ui/sidebar"
import { Navigation } from "./navigation"
import { useSidebar } from "@/lib/store"
import { cn } from "@/lib/utils"
import { ServerManagementModal } from "../server/server-management-modal"

export function AppSidebar() {
  // Zustand store (used by Navigation component)
  const { setCollapsed } = useSidebar()

  // SidebarProvider context (controls actual sidebar behaviour)
  const {
    state, // "expanded" | "collapsed" — desktop state machine only
    toggleSidebar,
    setOpenMobile,
    isMobile,
  } = useSidebarUI()

  // Keep the Zustand store in sync with the provider state.
  React.useEffect(() => {
    setCollapsed(state === "collapsed")
  }, [state, setCollapsed])

  // CC-32 (audit 2026-06-02): the previous auto-open effect watched
  // `[isMobile, state, setOpenMobile]` and called setOpenMobile(true)
  // whenever `state === 'expanded'`. `state` is the *desktop* expanded/
  // collapsed flag — it is NOT cleared when the user dismisses the
  // mobile sheet (the sheet uses the separate `openMobile` flag), so
  // the effect would re-fire on every subsequent render and re-open
  // the sheet, trapping the user behind an overlay with no visible
  // dismiss affordance (see below for the in-sheet close button that
  // closes the other half of this bug). Removed entirely — the
  // SidebarProvider's `defaultOpen` + the sheet's intrinsic
  // open/close state already give the right initial UX on mobile
  // (sheet starts closed; user opens via the header hamburger).

  const handleToggle = () => {
    toggleSidebar()
    // Zustand store will update via the effect above once state changes.
  }

  const collapsed = state === "collapsed"

  return (
    <Sidebar 
      variant="sidebar" 
      collapsible="icon"
      className={cn(
        "flex flex-col h-screen z-40 transition-all duration-300",
        collapsed && !isMobile ? "w-16" : "w-64"
      )}
    >
      {/* Sidebar Header */}
      <SidebarHeader className="border-b px-3 py-3">
        <div className="flex items-center justify-between">
          {(!collapsed || isMobile) && (
            <div className="flex items-center space-x-2">
              <div className="h-6 w-6 rounded bg-primary/20 flex items-center justify-center">
                <span className="text-xs font-semibold text-primary">M</span>
              </div>
              <span className="font-semibold text-sm text-foreground">MCP Control</span>
            </div>
          )}
          {!isMobile && (
            <Button
              variant="ghost"
              size="icon"
              onClick={handleToggle}
              className="h-8 w-8 shrink-0"
            >
              {collapsed ? (
                <PanelLeftOpen className="h-4 w-4" />
              ) : (
                <PanelLeftClose className="h-4 w-4" />
              )}
              <span className="sr-only">Toggle sidebar</span>
            </Button>
          )}
          {/* CC-32 (audit 2026-06-02): in-sheet close button for the
              mobile sheet variant. The header's hamburger trigger sits
              behind the SheetContent overlay's z-index once the sheet
              is open, and the shadcn <Sheet>'s built-in close X is
              hidden by the `[&>button]:hidden` selector applied in
              components/ui/sidebar.tsx (line 190). Without this button
              there is NO visible way to dismiss the sidebar on mobile.
              Sized 44x44 (h-11 w-11) to clear the 44 px touch-target
              floor — matches the in-content interactive elements. */}
          {isMobile && (
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setOpenMobile(false)}
              className="h-11 w-11 shrink-0"
              aria-label="Close sidebar"
            >
              <X className="h-5 w-5" />
              <span className="sr-only">Close sidebar</span>
            </Button>
          )}
        </div>
      </SidebarHeader>

      {/* Sidebar Content */}
      <SidebarContent className="px-0">
        <Navigation />
      </SidebarContent>

      {/* Sidebar Footer */}
      <SidebarFooter className="border-t p-3">
        <div className="flex items-center justify-between">
          {!collapsed && (
            <div className="text-xs text-muted-foreground">
              {/* CC-10 (audit 2026-06-02): dropped the "Improved
                  Dashboard" tagline — read as leftover beta-marketing
                  copy. Show product + version only. */}
              <div className="font-medium text-foreground">Agent-MCP</div>
              <div className="text-muted-foreground tabular-nums">v3.4.0</div>
            </div>
          )}
          <ServerManagementModal />
        </div>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  )
}