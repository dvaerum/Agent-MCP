"use client"

import React, { useState, useEffect } from "react"
import { Server, Settings, Wifi, Loader2, ExternalLink } from "lucide-react"
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

// Patched for the NixOS deployment. Upstream's picker switches
// between server-store entries (each = (host, port)). Our router
// addresses projects by URL path: /agent-mcp/__dashboard/<name>/.
// So the picker fetches the project list from the router and
// picking an entry navigates the browser instead of swapping a
// host:port pair.

const DASHBOARD_PREFIX = "/agent-mcp/__dashboard/"
const PROJECTS_ENDPOINT = "/agent-mcp/__projects"

function readActiveProjectName(): string | null {
  if (typeof window === "undefined") return null
  const m = window.location.pathname.match(/\/agent-mcp\/__dashboard\/([^/]+)/)
  return m ? m[1] : null
}

export function ProjectPicker() {
  const [isOpen, setIsOpen] = useState(false)
  const [projects, setProjects] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Deferred to a post-mount effect so SSG output ("Select
  // Project") matches the first client paint, avoiding the React
  // hydration mismatch (#418).
  const [active, setActive] = useState<string | null>(null)
  useEffect(() => {
    setActive(readActiveProjectName())
  }, [])

  const fetchProjects = async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await fetch(PROJECTS_ENDPOINT, { cache: "no-store" })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const body = await r.json()
      setProjects(Array.isArray(body.projects) ? body.projects : [])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchProjects()
  }, [])

  const handlePick = (name: string) => {
    if (name === active) {
      setIsOpen(false)
      return
    }
    window.location.href = `${DASHBOARD_PREFIX}${encodeURIComponent(name)}/`
  }

  return (
    <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" className="justify-between min-w-[200px]">
          <div className="flex items-center space-x-2">
            <div className="w-2 h-2 rounded-full bg-primary" />
            <span className="truncate">{active ?? "Select Project"}</span>
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
            onClick={() => fetchProjects()}
            className="h-6 w-6 p-0"
            disabled={loading}
          >
            {loading
              ? <Loader2 className="h-3 w-3 animate-spin" />
              : <Server className="h-3 w-3" />}
          </Button>
        </DropdownMenuLabel>

        <DropdownMenuSeparator />

        {error && (
          <div className="p-2 text-xs text-destructive">
            Failed to load projects: {error}
          </div>
        )}

        <div className="max-h-[300px] overflow-y-auto">
          {projects.length === 0 && !loading && !error && (
            <div className="p-3 text-xs text-muted-foreground">
              No projects registered. Visit{" "}
              <a href="/agent-mcp/" className="underline">
                /agent-mcp/
              </a>{" "}
              to create one.
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

        <DropdownMenuSeparator />

        <div className="p-2">
          <Button
            variant="outline"
            size="sm"
            asChild
            className="w-full"
          >
            <a href="/agent-mcp/">
              <ExternalLink className="h-4 w-4 mr-2" />
              Manage projects
            </a>
          </Button>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
