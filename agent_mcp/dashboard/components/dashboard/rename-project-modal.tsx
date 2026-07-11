"use client"

// Rename-project modal (Phase 3.5b — decision #4 + ADR-0010, alias-
// with-grace-period rename). PATCHes the router-admin REST resource
// at ``PATCH /agent-mcp/api/router/projects/<name>`` (ADR 0014); the
// body's ``name`` field carries the new slug.
//
// Fields:
//   * new_name   (required, slug)
//   * grace_days (optional, default 30) — how long the old name keeps
//                 working as an alias before the reaper expires it.
//                 Surfaced in the modal so power users can set a
//                 longer (or shorter) cutover window per project.
//
// Refuse paths surfaced inline:
//   * 409 active_sessions   — list current connection count.
//   * 409 name_taken        — duplicate project name.
//   * 409 alias_collision   — collides with an active alias.
//   * 400 (anything else)   — show the router's reason text verbatim.

import React, { useState } from "react"
import { appUrl, routerProjectUrl } from "@/lib/urls"
import { routerApi } from "@/lib/router-api"
import { Loader2, Pencil } from "lucide-react"
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

const SLUG_RE = /^[a-z](?:[a-z0-9-]*[a-z0-9])?$/

export interface RenameProjectModalProps {
  projectName: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function RenameProjectModal({
  projectName,
  open,
  onOpenChange,
}: RenameProjectModalProps): React.ReactElement {
  const [newName, setNewName] = useState("")
  const [graceDays, setGraceDays] = useState<string>("30")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fetchOverview = useProjectsStore((s) => s.fetchOverview)

  const reset = () => {
    setNewName("")
    setGraceDays("30")
    setError(null)
    setSubmitting(false)
  }
  const close = () => {
    reset()
    onOpenChange(false)
  }

  const validName = SLUG_RE.test(newName) && newName !== projectName
  const graceInt = Number.parseInt(graceDays, 10)
  const validGrace = Number.isFinite(graceInt) && graceInt >= 0 && graceInt <= 3650

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!validName) {
      setError(
        "New name must be a lowercase slug different from the current name.",
      )
      return
    }
    if (!validGrace) {
      setError("grace_days must be a non-negative integer ≤ 3650.")
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      // ADR 0014: PATCH /api/router/projects/<name> with JSON body
      // {name, grace_days}. The unified envelope's ``message`` field
      // is the human-readable error string; ``error`` is the
      // discriminator code.
      await routerApi.request(routerProjectUrl(projectName), {
        method: "PATCH",
        body: JSON.stringify({
          name: newName,
          grace_days: graceInt,
        }),
      })
      await fetchOverview()
      close()
      // Navigate to the new project URL so the URL bar matches the
      // renamed entry. The old URL still works (grace alias), but the
      // dashboard's project-context is keyed on path, so without this
      // the next page-load would render the old name.
      if (typeof window !== "undefined") {
        // appUrl already encodeURIComponent's the project name.
        window.location.href = appUrl(newName)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? onOpenChange(true) : close())}>
      <DialogContent>
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Pencil className="h-4 w-4" />
              Rename <code className="text-base">{projectName}</code>
            </DialogTitle>
            <DialogDescription>
              Renames the project end-to-end (workspace dir moved, systemd
              unit recreated). The old name keeps working as an alias for
              the grace period below, then expires.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="rename-new-name">New name</Label>
              <Input
                id="rename-new-name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="new-project-name"
                autoFocus
                required
              />
              {newName && !validName && (
                <p className="text-xs text-destructive">
                  Must be a lowercase slug different from the current name.
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="rename-grace-days">Alias grace period (days)</Label>
              <Input
                id="rename-grace-days"
                type="number"
                min={0}
                max={3650}
                value={graceDays}
                onChange={(e) => setGraceDays(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Old URLs + MCP configs keep working for this many days.
                After that the alias expires and the old name 404s.
              </p>
            </div>

            {error && (
              <div className="p-2 rounded bg-destructive/10 text-sm text-destructive whitespace-pre-wrap">
                {error}
              </div>
            )}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={close}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting || !validName || !validGrace}>
              {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Rename
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
