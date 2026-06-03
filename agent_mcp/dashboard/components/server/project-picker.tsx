"use client"

import React, { useState, useEffect } from "react"
import { Server, Settings, Wifi, Loader2, ArrowLeft, Lock } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { useProjectsStore } from "@/lib/stores/projects-store"
import { APP_PROJECT_PATH_RE, appUrl, overviewAppUrl } from "@/lib/urls"

// Patched for the NixOS deployment. Upstream's picker switches
// between server-store entries (each = (host, port)). Our router
// addresses projects by URL path: /agent-mcp/app/<name>/ (PR-B
// renamed from /__dashboard/<name>/). So the picker fetches the
// project list from the router and picking an entry navigates the
// browser instead of swapping a host:port pair.
//
// Phase 3.5b (prancy-napping-pie decision #10): the picker now reads
// from the new useProjectsStore (backed by /__overview) so it can
// learn the router's tenancy mode in the same payload. Behaviour:
//
//   * Multi-tenant: prepend an "← All projects" entry that navigates
//     to /agent-mcp/app/ (the cross-project overview).
//   * Single-tenant: the dropdown is disabled, showing only the
//     configured project name with a small "single-tenant" badge.
//     There's nowhere else for the operator to go.

function readActiveProjectName(): string | null {
  if (typeof window === "undefined") return null
  const m = window.location.pathname.match(APP_PROJECT_PATH_RE)
  return m ? m[1] : null
}

export function ProjectPicker() {
  const [isOpen, setIsOpen] = useState(false)
  const { envelope, loading, error, fetchOverview } = useProjectsStore()

  // Deferred to a post-mount effect so SSG output ("Select
  // Project") matches the first client paint, avoiding the React
  // hydration mismatch (#418).
  const [active, setActive] = useState<string | null>(null)
  useEffect(() => {
    setActive(readActiveProjectName())
  }, [])

  useEffect(() => {
    if (!envelope && !loading) {
      void fetchOverview()
    }
  }, [envelope, loading, fetchOverview])

  const multiTenant = envelope?.multi_tenant !== false
  const singleName = envelope?.single_tenant_name ?? null
  const projects = envelope?.projects.map((p) => p.name) ?? []
  const displayName = active ?? singleName ?? "Select Project"

  const handlePick = (name: string) => {
    if (name === active) {
      setIsOpen(false)
      return
    }
    window.location.href = appUrl(name)
  }

  // Single-tenant: render a disabled button that only shows the
  // project name. No dropdown, no actions — there's exactly one
  // project, statically configured by the operator's home-manager
  // profile.
  if (envelope && !multiTenant) {
    return (
      <Button
        variant="outline"
        className="justify-between min-w-[200px] cursor-not-allowed opacity-80"
        disabled
        aria-label="Project picker disabled in single-tenant mode"
      >
        <div className="flex items-center space-x-2">
          <Lock className="h-3 w-3 text-muted-foreground" />
          <span className="truncate">{displayName}</span>
        </div>
        <Badge variant="outline" className="text-[10px] ml-2">
          single-tenant
        </Badge>
      </Button>
    )
  }

  return (
    <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" className="justify-between min-w-[200px]">
          <div className="flex items-center space-x-2">
            <div className="w-2 h-2 rounded-full bg-primary" />
            <span className="truncate">{displayName}</span>
          </div>
          <Settings className="h-4 w-4 ml-2 opacity-50" />
        </Button>
      </DropdownMenuTrigger>

      <DropdownMenuContent className="w-80" align="start">
        <DropdownMenuLabel className="flex items-center justify-between">
          MCP Server Projects
          <Button
            variant="ghost"
            size="sm"
            onClick={() => fetchOverview()}
            className="h-6 w-6 p-0"
            disabled={loading}
          >
            {loading
              ? <Loader2 className="h-3 w-3 animate-spin" />
              : <Server className="h-3 w-3" />}
          </Button>
        </DropdownMenuLabel>

        <DropdownMenuSeparator />

        {/* "← All projects" entry — only in multi-tenant mode (decision #10). */}
        <DropdownMenuItem
          onClick={() => (window.location.href = overviewAppUrl())}
          className="flex items-center p-3 cursor-pointer"
        >
          <ArrowLeft className="h-4 w-4 mr-3 text-muted-foreground" />
          <span className="font-medium">All projects</span>
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        {error && (
          <div className="p-2 text-xs text-destructive">
            Failed to load projects: {error}
          </div>
        )}

        <div className="max-h-[300px] overflow-y-auto">
          {projects.length === 0 && !loading && !error && (
            <div className="p-3 text-xs text-muted-foreground">
              No projects registered. Use the{" "}
              <a href={overviewAppUrl()} className="underline">
                overview
              </a>{" "}
              to add one.
            </div>
          )}
          {projects.map((name) => (
            <DropdownMenuItem
              key={name}
              onClick={() => handlePick(name)}
              className="flex items-center justify-between p-3 cursor-pointer"
            >
              <div className="flex items-center space-x-3">
                <Wifi className="h-4 w-4 text-primary" />
                <span className="font-medium">{name}</span>
              </div>
              {name === active && (
                <Badge variant="secondary" className="text-xs">
                  Current
                </Badge>
              )}
            </DropdownMenuItem>
          ))}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
