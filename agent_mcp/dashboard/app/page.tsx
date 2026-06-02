"use client"

import { Suspense } from "react"
import { MainLayout } from "@/components/layout/main-layout"
import { DashboardWrapper } from "@/components/dashboard/dashboard-wrapper"
import { OverviewDashboard } from "@/components/dashboard/overview-dashboard"
import { AgentsDashboard } from "@/components/dashboard/agents-dashboard"
import { TasksDashboard } from "@/components/dashboard/tasks-dashboard"
import { MemoriesDashboard } from "@/components/dashboard/memories-dashboard"
import { MessagesDashboard } from "@/components/dashboard/messages-dashboard"
import { SettingsDashboard } from "@/components/dashboard/settings-dashboard"
import { PromptBookDashboard } from "@/components/dashboard/prompt-book-dashboard"
import { SystemDashboard } from "@/components/dashboard/system-dashboard"
import { useSectionRoute } from "@/lib/use-section-route"

function DashboardPage() {
  // URL-driven section routing — `?page=<section>` is the source of
  // truth. Reload + share-links land on the same section the user was
  // last looking at. Missing/unknown values fall back to 'overview'.
  const { currentSection } = useSectionRoute()

  const renderCurrentView = () => {
    switch (currentSection) {
      case 'overview':
        return <OverviewDashboard />
      case 'agents':
        return <AgentsDashboard />
      case 'tasks':
        return <TasksDashboard />
      case 'memories':
        return <MemoriesDashboard />
      case 'messages':
        return <MessagesDashboard />
      case 'settings':
        return <SettingsDashboard />
      case 'prompts':
        return <PromptBookDashboard />
      default:
        return <OverviewDashboard />
    }
  }

  return (
    <MainLayout>
      <DashboardWrapper>
        {renderCurrentView()}
      </DashboardWrapper>
    </MainLayout>
  )
}

// useSearchParams() forces this page into the client-rendered branch
// at build time. Next.js requires a <Suspense> boundary around any
// component that calls useSearchParams so the static-export build
// doesn't bail out — wrap once here.
export default function HomePage() {
  return (
    <Suspense fallback={null}>
      <DashboardPage />
    </Suspense>
  )
}
