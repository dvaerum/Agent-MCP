"use client"

// Cross-project overview dashboard (Phase 3.5a — prancy-napping-pie).
// Lives at `/agent-mcp/__dashboard/` (no project segment). Renders one
// card per registered project (R2 + S2 + multi-line per the locked
// design table) backed by the new `/agent-mcp/__overview` router
// endpoint via `useProjectsStore`.
//
// Per-card layout (multi-line, ~2-3 visible lines + "Show details"
// toggle):
//
//   ┌────────────────────────────────────────────────────────────┐
//   │  <name>                                       [STATUS chip]│
//   │  Last activity: 32s ago   Agents: 3   Tasks: 12   Msgs: 2  │
//   │  [alias: oldname (expires 2026-07-15)] [Show details ▼]    │
//   │                                                            │
//   │  --- expanded ---                                          │
//   │  Workspace: /home/dennis/.local/share/agent-mcp/projects/x │
//   └────────────────────────────────────────────────────────────┘
//
// Add/Remove/Rename modal buttons wired in PR-B (Phase 3.5b). Alias
// chip expansion + wiring snippets ship in PR-C (Phase 3.5c).

import React, { useEffect, useState } from "react"
import {
  RefreshCw, Folder, Loader2, AlertCircle, ChevronDown, ChevronUp,
  Plus, Pencil, Trash2, MoreHorizontal,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  useProjectsStore,
  type ProjectOverviewRow,
  type ProjectStatus,
} from "@/lib/stores/projects-store"
import { AddProjectModal } from "./add-project-modal"
import { RemoveProjectModal } from "./remove-project-modal"
import { RenameProjectModal } from "./rename-project-modal"

const STATUS_VARIANT: Record<ProjectStatus, "default" | "secondary" | "destructive" | "outline"> = {
  active: "default",
  idle: "secondary",
  sleeping: "outline",
  stopped: "outline",
  starting: "secondary",
  failed: "destructive",
}

const STATUS_DOT_COLOUR: Record<ProjectStatus, string> = {
  active: "bg-emerald-500",
  idle: "bg-amber-500",
  sleeping: "bg-slate-400",
  stopped: "bg-slate-300",
  starting: "bg-blue-500",
  failed: "bg-red-500",
}

function formatRelative(ts: number | null): string {
  if (ts === null) return "never"
  const ageSec = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (ageSec < 60) return `${ageSec}s ago`
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`
  if (ageSec < 86400) return `${Math.floor(ageSec / 3600)}h ago`
  return `${Math.floor(ageSec / 86400)}d ago`
}

function ProjectCard({
  row,
  multiTenant,
}: {
  row: ProjectOverviewRow
  multiTenant: boolean
}): React.ReactElement {
  const [showDetails, setShowDetails] = useState(false)
  const [renameOpen, setRenameOpen] = useState(false)
  const [removeOpen, setRemoveOpen] = useState(false)
  const dashboardHref = `/agent-mcp/__dashboard/${encodeURIComponent(row.name)}/`
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
        <div className="flex items-center space-x-2">
          <div
            className={`w-2 h-2 rounded-full ${STATUS_DOT_COLOUR[row.status]}`}
          />
          <CardTitle className="text-base">
            <a
              href={dashboardHref}
              className="font-medium hover:underline"
            >
              {row.name}
            </a>
          </CardTitle>
        </div>
        <div className="flex items-center gap-1">
          <Badge variant={STATUS_VARIANT[row.status]} className="text-xs capitalize">
            {row.status}
          </Badge>
          {multiTenant && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  aria-label={`Project ${row.name} actions`}
                >
                  <MoreHorizontal className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => setRenameOpen(true)}>
                  <Pencil className="h-4 w-4 mr-2" />
                  Rename
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={() => setRemoveOpen(true)}
                  className="text-destructive focus:text-destructive"
                >
                  <Trash2 className="h-4 w-4 mr-2" />
                  Remove
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </CardHeader>
      <CardContent className="pb-3 space-y-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>
            Last activity:{" "}
            <span className="text-foreground">
              {formatRelative(row.last_activity_ts)}
            </span>
          </span>
          <span>
            Agents: <span className="text-foreground">{row.agents}</span>
          </span>
          <span>
            Tasks: <span className="text-foreground">{row.tasks}</span>
          </span>
          <span>
            Messages: <span className="text-foreground">{row.open_messages}</span>
          </span>
        </div>

        {row.alias.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {row.alias.map((a) => (
              <Badge key={a.name} variant="outline" className="text-[10px]">
                alias <code className="px-1">{a.name}</code> → expires{" "}
                {a.expires_at.slice(0, 10)}
              </Badge>
            ))}
          </div>
        )}

        <div className="flex items-center justify-between pt-1">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 text-xs"
            onClick={() => setShowDetails((v) => !v)}
          >
            {showDetails ? (
              <>
                <ChevronUp className="h-3 w-3 mr-1" />
                Hide details
              </>
            ) : (
              <>
                <ChevronDown className="h-3 w-3 mr-1" />
                Show details
              </>
            )}
          </Button>
          <Button asChild variant="outline" size="sm" className="h-7 text-xs">
            <a href={dashboardHref}>Open</a>
          </Button>
        </div>

        {showDetails && (
          <div className="pt-2 border-t mt-2 space-y-1 text-xs">
            <div className="flex items-center gap-1 text-muted-foreground">
              <Folder className="h-3 w-3" />
              <span>Workspace:</span>
            </div>
            <code className="block bg-muted px-2 py-1 rounded text-[11px] break-all">
              {row.workspace}
            </code>
          </div>
        )}
      </CardContent>
      <RenameProjectModal
        projectName={row.name}
        open={renameOpen}
        onOpenChange={setRenameOpen}
      />
      <RemoveProjectModal
        projectName={row.name}
        open={removeOpen}
        onOpenChange={setRemoveOpen}
      />
    </Card>
  )
}

export function ProjectsOverviewDashboard(): React.ReactElement {
  const { envelope, loading, error, fetchOverview } = useProjectsStore()
  const [addOpen, setAddOpen] = useState(false)
  const multiTenant = envelope?.multi_tenant !== false

  useEffect(() => {
    fetchOverview()
  }, [fetchOverview])

  // Refresh on MCP `notifications/resources/updated`. The provider
  // (PR #81) dispatches a `window` event we can hook into without
  // importing the provider module (which would bring along a per-
  // project API client we don't want loaded in overview mode).
  useEffect(() => {
    if (typeof window === "undefined") return
    const handler = () => {
      fetchOverview()
    }
    window.addEventListener("mcp:resources-updated", handler)
    return () => window.removeEventListener("mcp:resources-updated", handler)
  }, [fetchOverview])

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="max-w-6xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              Agent MCP — Projects
            </h1>
            <p className="text-sm text-muted-foreground">
              {envelope?.multi_tenant === false
                ? `Single-tenant mode (${envelope.single_tenant_name})`
                : "All registered projects on this router."}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {multiTenant && (
              <Button
                variant="default"
                size="sm"
                onClick={() => setAddOpen(true)}
              >
                <Plus className="h-4 w-4 mr-2" />
                Add project
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              disabled={loading}
              onClick={() => fetchOverview()}
            >
              {loading ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4 mr-2" />
              )}
              Refresh
            </Button>
          </div>
        </div>

        <AddProjectModal open={addOpen} onOpenChange={setAddOpen} />

        {error && (
          <Card>
            <CardContent className="flex items-center gap-2 py-4 text-sm text-destructive">
              <AlertCircle className="h-4 w-4" />
              Failed to load projects: {error}
            </CardContent>
          </Card>
        )}

        {envelope && envelope.projects.length === 0 && !loading && !error && (
          <Card>
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              No projects registered yet.
            </CardContent>
          </Card>
        )}

        {envelope && envelope.projects.length > 0 && (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {envelope.projects.map((row) => (
              <ProjectCard
                key={row.name}
                row={row}
                multiTenant={multiTenant}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
