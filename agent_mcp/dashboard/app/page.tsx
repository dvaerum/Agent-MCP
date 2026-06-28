"use client"

import { Suspense } from "react"
import dynamic from "next/dynamic"
import { MainLayout } from "@/components/layout/main-layout"
import { DashboardWrapper } from "@/components/dashboard/dashboard-wrapper"
import { Skeleton } from "@/components/ui/skeleton"
import { useSectionRoute } from "@/lib/use-section-route"
import { projectContext } from "@/lib/project-context"

// Per-section code-splitting — each section dashboard ships in its own
// JS chunk and is fetched on demand when the user lands on (or
// navigates to) that section. The previous static imports pulled all
// nine section trees into the `/page-*.js` initial bundle (~321 KB),
// which dominated mobile cold-load parse time. After this split the
// first-load bundle only carries the layout shell + the section the
// user actually opened; switching sections pays a one-time ~100 ms
// chunk fetch — the standard SPA trade.
//
// `ssr: false` keeps the static-export build honest: every section
// already uses `"use client"` (zustand stores, browser-only APIs), so
// there is no SSR value to preserve, and the prerender step would
// otherwise drag the trees back into the server bundle.
//
// `loading: () => <SectionSkeleton />` reuses the project's shared
// `Skeleton` primitive (components/ui/skeleton.tsx) — same animation,
// same theme tokens — so the placeholder doesn't fight the section
// chrome the user is about to see.

function SectionSkeleton() {
  return (
    <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6 flex flex-col h-full">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 shrink-0">
        <div className="space-y-2">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-72" />
        </div>
      </div>
      <Skeleton className="flex-1 min-h-[400px] rounded-lg" />
    </div>
  )
}

const OverviewDashboard = dynamic(
  () => import("@/components/dashboard/overview-dashboard").then(m => m.OverviewDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)
const ProjectsOverviewDashboard = dynamic(
  () => import("@/components/dashboard/projects-overview-dashboard").then(m => m.ProjectsOverviewDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)
const AgentsDashboard = dynamic(
  () => import("@/components/dashboard/agents-dashboard").then(m => m.AgentsDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)
const TasksDashboard = dynamic(
  () => import("@/components/dashboard/tasks-dashboard").then(m => m.TasksDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)
const MemoriesDashboard = dynamic(
  () => import("@/components/dashboard/memories-dashboard").then(m => m.MemoriesDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)
const MessagesDashboard = dynamic(
  () => import("@/components/dashboard/messages-dashboard").then(m => m.MessagesDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)
const SettingsDashboard = dynamic(
  () => import("@/components/dashboard/settings-dashboard").then(m => m.SettingsDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)
const PromptBookDashboard = dynamic(
  () => import("@/components/dashboard/prompt-book-dashboard").then(m => m.PromptBookDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)
const SystemDashboard = dynamic(
  () => import("@/components/dashboard/system-dashboard").then(m => m.SystemDashboard),
  { ssr: false, loading: () => <SectionSkeleton /> },
)

function DashboardPage() {
  // URL-driven section routing — `?page=<section>` is the source of
  // truth. Reload + share-links land on the same section the user was
  // last looking at. Missing/unknown values fall back to 'overview'.
  // (Hook must be called unconditionally; the overview branch below
  // simply ignores its value.)
  const { currentSection } = useSectionRoute()

  // Phase 3.5a — when the URL is `/agent-mcp/app/` (no project
  // segment; PR-B renamed from /__dashboard/), render the
  // cross-project overview instead of the
  // per-project dashboard. The MainLayout is skipped because the
  // overview has its own header (no sidebar nav, no project picker
  // for self — picker enhancements ship in PR-B for per-project pages).
  if (projectContext.isOverview) {
    return <ProjectsOverviewDashboard />
  }

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
      case 'system':
        return <SystemDashboard />
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
