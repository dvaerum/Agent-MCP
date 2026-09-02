"use client"

import React, { useMemo } from "react"
import {
  Activity,
  Brain,
  CheckCircle2,
  Cpu,
  ListTodo,
  RefreshCw,
  Server,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useServerStore } from "@/lib/stores/server-store"
import { useAllData, useAllDataStatus } from "@/lib/queries/all-data"

// Render an ISO timestamp as a coarse relative-time string ("5m ago",
// "2h ago"). The Overview uses this for the recent-activity feed and
// the per-card "last added" hints. Falls back to the raw value if the
// timestamp can't be parsed.
function relativeTime(iso: string | undefined | null): string {
  if (!iso) return "—"
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const deltaSec = Math.max(1, Math.round((Date.now() - t) / 1000))
  if (deltaSec < 60) return `${deltaSec}s ago`
  const min = Math.round(deltaSec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.round(min / 60)
  if (hr < 24) return `${hr}h ago`
  const day = Math.round(hr / 24)
  return `${day}d ago`
}

// Stat-card primitive — title, big number, optional sub-line, and an
// icon. The mobile-load PR replaced the full collaboration graph on
// this page with a handful of these so the cold-load doesn't drag in
// the 617 KB graph-library chunk just to render the landing page.
// That graph (and the System page that hosted it) was later removed
// entirely.
function StatCard({
  title,
  icon: Icon,
  primary,
  sub,
}: {
  title: string
  icon: React.ComponentType<{ className?: string }>
  primary: React.ReactNode
  sub?: React.ReactNode
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold tabular-nums">{primary}</div>
        {sub != null && (
          <p className="text-xs text-muted-foreground mt-1">{sub}</p>
        )}
      </CardContent>
    </Card>
  )
}

// Action shape returned in AllData.actions. Kept loose because the
// backend payload includes optional task/agent linkage fields whose
// exact name varies by action type.
interface ActionRow {
  action_id?: string
  agent_id?: string
  action_type?: string | null
  task_id?: string | null
  timestamp?: string
  details?: unknown
}

export function OverviewDashboard() {
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  // Wave 6 keystone increment 1: reads the shared `/all-data` TanStack
  // Query. The query fetches automatically once a connected server is
  // selected, so no mount effect is needed; `refresh` is the awaitable
  // force-refetch behind the manual Refresh button.
  const data = useAllData()
  const { loading, isRefreshing, refresh } = useAllDataStatus()

  const isConnected = !!activeServerId && activeServer?.status === 'connected'

  // Derived stat-card numbers. Memoised so the recent-activity feed
  // below doesn't recompute these on its own re-renders.
  const stats = useMemo(() => {
    if (!data) {
      return {
        agentsTotal: 0,
        agentsActive: 0,
        tasksTotal: 0,
        tasksInProgress: 0,
        tasksCompleted: 0,
        memoriesTotal: 0,
        memoriesLastUpdated: undefined as string | undefined,
        actionsTotal: 0,
        actionsRecent: 0,
      }
    }
    const agents = data.agents ?? []
    const tasks = data.tasks ?? []
    const context = (data.context ?? []) as Array<{ updated_at?: string }>
    const actions = (data.actions ?? []) as ActionRow[]

    const agentsActive = agents.filter(
      a => a.status === 'running' || a.status === 'pending',
    ).length
    const tasksInProgress = tasks.filter(t => t.status === 'in_progress').length
    const tasksCompleted = tasks.filter(t => t.status === 'completed').length

    const memoriesLastUpdated = context
      .map((c) => c.updated_at)
      .filter((ts): ts is string => typeof ts === 'string' && ts.length > 0)
      .sort()
      .pop()

    // "Recent" = actions inside the last hour. Cheap heuristic — the
    // exact threshold doesn't matter for the sub-line; it just gives
    // operators a sense of whether the system is doing anything right
    // now vs. cold-stored history.
    const oneHourAgo = Date.now() - 60 * 60 * 1000
    const actionsRecent = actions.filter(a => {
      if (!a.timestamp) return false
      const t = Date.parse(a.timestamp)
      return !Number.isNaN(t) && t >= oneHourAgo
    }).length

    return {
      agentsTotal: agents.length,
      agentsActive,
      tasksTotal: tasks.length,
      tasksInProgress,
      tasksCompleted,
      memoriesTotal: context.length,
      memoriesLastUpdated,
      actionsTotal: actions.length,
      actionsRecent,
    }
  }, [data])

  // Recent-activity feed — last 10 actions, newest first. The
  // `agent_actions` table is included in `getAllData` so we don't add
  // a new API call. Each row renders the action verb + the agent +
  // the task id if present + relative time.
  const recentActivity = useMemo<ActionRow[]>(() => {
    const actions = (data?.actions ?? []) as ActionRow[]
    return [...actions]
      .filter(a => a.timestamp)
      .sort((a, b) => {
        const ta = Date.parse(a.timestamp ?? '')
        const tb = Date.parse(b.timestamp ?? '')
        return tb - ta
      })
      .slice(0, 10)
  }, [data?.actions])

  if (!isConnected) {
    return (
      <div className="h-full flex items-center justify-center p-4">
        <Card className="max-w-md">
          <CardContent className="flex flex-col items-center justify-center py-12 px-8 text-center">
            <Server className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-medium text-foreground mb-2">Connect to an MCP Server</h3>
            <p className="text-muted-foreground text-sm">
              Select an MCP server from the project picker in the header to view activity and manage agents.
            </p>
            {activeServer && activeServer.status === 'error' && (
              <div className="text-sm text-destructive mt-4">
                Failed to connect to {activeServer.name} ({activeServer.baseUrl ?? `${activeServer.host}:${activeServer.port}`})
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6 flex flex-col h-full overflow-y-auto">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 shrink-0">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-foreground">Overview</h1>
          <p className="text-muted-foreground text-sm sm:text-base mt-1">
            Snapshot of the agents, tasks, memories, and recent activity in this project.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          <Badge variant="outline" className="text-xs bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30 font-medium">
            <span aria-hidden className="w-2 h-2 bg-emerald-500 rounded-full mr-2" />
            {activeServer?.name}
          </Badge>
          {data?.timestamp && (
            <span className="text-xs text-muted-foreground tabular-nums">
              Last updated: {new Date(data.timestamp).toLocaleTimeString()}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => { void refresh() }}
            disabled={loading || isRefreshing}
            className="text-xs"
          >
            <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${(loading || isRefreshing) ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
        <StatCard
          title="Agents"
          icon={Cpu}
          primary={stats.agentsTotal}
          sub={`${stats.agentsActive} active`}
        />
        <StatCard
          title="Tasks"
          icon={ListTodo}
          primary={stats.tasksTotal}
          sub={
            <>
              {stats.tasksInProgress} in progress · {stats.tasksCompleted} completed
            </>
          }
        />
        <StatCard
          title="Memories"
          icon={Brain}
          primary={stats.memoriesTotal}
          sub={
            stats.memoriesLastUpdated
              ? `last added ${relativeTime(stats.memoriesLastUpdated)}`
              : "none yet"
          }
        />
        <StatCard
          title="Activity"
          icon={Activity}
          primary={stats.actionsTotal}
          sub={`${stats.actionsRecent} in the last hour`}
        />
      </div>

      {/* Recent activity feed */}
      <div className="grid grid-cols-1 gap-4 sm:gap-6 flex-1 min-h-0">
        <Card className="flex flex-col">
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Recent activity</CardTitle>
          </CardHeader>
          <CardContent className="flex-1 overflow-y-auto">
            {recentActivity.length === 0 ? (
              <div className="text-sm text-muted-foreground py-8 text-center">
                No agent activity recorded yet.
              </div>
            ) : (
              <ul className="space-y-2.5">
                {recentActivity.map((action, idx) => {
                  const verb = (action.action_type ?? 'action').replace(/_/g, ' ')
                  const agent = action.agent_id ?? 'unknown'
                  const task = action.task_id
                  const isCompletion =
                    action.action_type === 'task_completed' ||
                    action.action_type === 'complete_task'
                  return (
                    <li
                      key={action.action_id ?? `${action.timestamp ?? ''}-${idx}`}
                      className="flex items-start gap-3 text-sm border-l-2 pl-3 py-1 border-muted hover:border-primary/50 transition-colors"
                    >
                      {isCompletion ? (
                        <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0 mt-0.5" />
                      ) : (
                        <Activity className="h-4 w-4 text-muted-foreground shrink-0 mt-0.5" />
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="flex flex-wrap items-baseline gap-x-2">
                          <span className="font-medium text-foreground truncate">{agent}</span>
                          <span className="text-muted-foreground">{verb}</span>
                          {task && (
                            <code className="text-xs bg-muted px-1 py-0.5 rounded truncate max-w-[12rem]">
                              {task}
                            </code>
                          )}
                        </div>
                        <span className="text-xs text-muted-foreground tabular-nums">
                          {relativeTime(action.timestamp)}
                        </span>
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
