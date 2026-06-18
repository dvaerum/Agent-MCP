"use client"

// Add-project modal (Phase 3.5b — prancy-napping-pie decision #7,
// "C2"). Two fields:
//
//   * name (required)       — slug; validated against the same regex
//                             the router uses (^[a-z][a-z0-9-]*[a-z0-9]?$)
//   * workspace (optional)  — editable path; blank → router uses
//                             DEFAULT_WORKSPACE_PARENT/<name>
//
// POSTs a JSON body to the router-admin REST resource at
// ``POST /agent-mcp/api/router/projects`` (ADR 0014). The session
// cookie carries auth — the dashboard sends no token field. On
// success we refresh the overview store; on 4xx we surface the
// router's envelope ``message``.

import React, { useState } from "react"
import { Loader2, Plus } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useProjectsStore } from "@/lib/stores/projects-store"
import { routerProjectsUrl } from "@/lib/urls"

const SLUG_RE = /^[a-z](?:[a-z0-9-]*[a-z0-9])?$/

export interface AddProjectModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function AddProjectModal({
  open,
  onOpenChange,
}: AddProjectModalProps): React.ReactElement {
  const [name, setName] = useState("")
  const [workspace, setWorkspace] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fetchOverview = useProjectsStore((s) => s.fetchOverview)

  const resetAndClose = () => {
    setName("")
    setWorkspace("")
    setError(null)
    setSubmitting(false)
    onOpenChange(false)
  }

  const validName = SLUG_RE.test(name)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!validName) {
      setError(
        "Name must start with a lowercase letter and contain only " +
          "lowercase letters, digits, and hyphens.",
      )
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const r = await fetch(routerProjectsUrl(), {
        method: "POST",
        body: JSON.stringify({ name }),
        headers: {
          "Accept": "application/vnd.agent-mcp.v1+json",
          "Content-Type": "application/json",
        },
      })
      const body = await r.json().catch(() => ({} as any))
      if (!r.ok || body.success === false) {
        throw new Error(
          body.message || body.error || `HTTP ${r.status}`,
        )
      }
      await fetchOverview()
      resetAndClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Plus className="h-4 w-4" />
              Add a new project
            </DialogTitle>
            <DialogDescription>
              Register a new agent-mcp project on this router. Leave the
              workspace blank to let the router create one under the
              default location.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="add-project-name">Project name</Label>
              <Input
                id="add-project-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-project"
                autoFocus
                required
              />
              {name && !validName && (
                <p className="text-xs text-destructive">
                  Lowercase slug only: ^[a-z][a-z0-9-]*[a-z0-9]?$
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="add-project-workspace">
                Workspace path <span className="text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="add-project-workspace"
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                placeholder="/home/dennis/.local/share/agent-mcp/projects/<name>"
              />
              <p className="text-xs text-muted-foreground">
                Editable for the "restore from existing folder" use case.
              </p>
            </div>

            {error && (
              <p className="text-sm text-destructive whitespace-pre-wrap">
                {error}
              </p>
            )}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={resetAndClose}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting || !validName}>
              {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
