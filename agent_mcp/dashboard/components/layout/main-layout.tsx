"use client"

import React from "react"
import { Header } from "./header"
import { AppSidebar } from "./app-sidebar"
import { SidebarProvider } from "@/components/ui/sidebar"

interface MainLayoutProps {
  children: React.ReactNode
}

export function MainLayout({ children }: MainLayoutProps) {
  // Theme (incl. live OS `system`-mode follow) is owned by the app-wide
  // ThemeProvider in app/layout.tsx — this layout no longer duplicates
  // that media-query listener (AF-B: the two copies were redundant).
  return (
    <SidebarProvider defaultOpen={true}>
      <div className="relative h-screen bg-background flex overflow-hidden w-full">
        {/* Sidebar */}
        <AppSidebar />
        
        {/* Main Content Area */}
        <div className="flex-1 flex flex-col h-full overflow-hidden">
          {/* Header */}
          <Header />
          
          {/* Main Content */}
          <main className="flex-1 overflow-auto min-h-0">
            {/* CC-15 audit 2026-06-02: dropped the layout-level
                page-fade animation. Combined with shadcn dialog enters
                + sidebar tooltip animations it read as busy motion
                against the modern-minimal target. Component-level
                150ms hover/focus transitions own the motion budget. */}
            <div className="fluid-container h-full">
              {children}
            </div>
          </main>
        </div>
      </div>
    </SidebarProvider>
  )
}