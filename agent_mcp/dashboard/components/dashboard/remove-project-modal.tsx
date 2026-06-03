"use client"

// Remove-project modal (Phase 3.5b — decision #8, "D4 two-tier
// safe-default"). Default flow:
//
//   * unregister + stop systemd; workspace files left intact.
//
// Opt-in destructive flow:
//
//   * checkbox "Also delete workspace files (irreversible)"
//   * confirmation: must type the project name verbatim
//   * sends ``delete_workspace=true`` to the existing __unregister
//     endpoint (extended in this PR to honour the flag).
//
// Refuse path: router returns 409 with
// ``{error: "active_sessions", active_connections: N}`` when the
// project has any in-flight MCP/REST traffic. We surface the count
// and ask the operator to disconnect agents before retrying.

import React, { useState } from "react"
import { AlertTriangle, Loader2, Trash2 } from "lucide-react"
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

export interface RemoveProjectModalProps {
  projectName: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function RemoveProjectModal({
  projectName,
  open,
  onOpenChange,
}: RemoveProjectModalProps): React.ReactElement {
  const [deleteWorkspace, setDeleteWorkspace] = useState(false)
  const [confirmName, setConfirmName] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeConns, setActiveConns] = useState<number | null>(null)
  const fetchOverview = useProjectsStore((s) => s.fetchOverview)

  const reset = () => {
    setDeleteWorkspace(false)
    setConfirmName("")
    setError(null)
    setSubmitting(false)
    setActiveConns(null)
  }
  const close = () => {
    reset()
    onOpenChange(false)
  }

  const destructiveReady =
    !deleteWorkspace || confirmName === projectName

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    setActiveConns(null)
    try {
      const body = new URLSearchParams()
      body.set("name", projectName)
      if (deleteWorkspace) body.set("delete_workspace", "true")
      const r = await fetch("/agent-mcp/__unregister", {
        method: "POST",
        body,
        headers: { Accept: "application/json" },
        redirect: "manual",
      })
      if (r.status === 409) {
        const detail = await r.json().catch(() => ({}))
        setActiveConns(
          typeof detail.active_connections === "number"
            ? detail.active_connections
            : 0,
        )
        setError(
          typeof detail.reason === "string"
            ? detail.reason
            : "Active sessions block removal.",
        )
        setSubmitting(false)
        return
      }
      if (r.type !== "opaqueredirect" && r.status >= 400) {
        const text = await r.text().catch(() => "")
        throw new Error(text || `HTTP ${r.status}`)
      }
      await fetchOverview()
      close()
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
              <Trash2 className="h-4 w-4" />
              Remove project <code className="text-base">{projectName}</code>
            </DialogTitle>
            <DialogDescription>
              Stops the systemd backend and drops the project from the
              router registry. Workspace files are kept by default;
              opt in below if you want them wiped too.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={deleteWorkspace}
                onChange={(e) => setDeleteWorkspace(e.target.checked)}
                className="mt-1"
              />
              <span>
                <span className="font-medium">
                  Also delete workspace files (irreversible)
                </span>
                <span className="block text-xs text-muted-foreground">
                  Recursively removes the project's workspace directory.
                  Only allowed when the workspace lives under the
                  router's default workspace parent.
                </span>
              </span>
            </label>

            {deleteWorkspace && (
              <div className="space-y-2 border-l-2 border-destructive pl-3">
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertTriangle className="h-4 w-4" />
                  Type <code className="px-1">{projectName}</code> to confirm.
                </div>
                <Label htmlFor="confirm-name" className="sr-only">
                  Confirm project name
                </Label>
                <Input
                  id="confirm-name"
                  value={confirmName}
                  onChange={(e) => setConfirmName(e.target.value)}
                  placeholder={projectName}
                  autoFocus
                />
              </div>
            )}

            {error && (
              <div className="p-2 rounded bg-destructive/10 text-sm text-destructive">
                <div>{error}</div>
                {activeConns !== null && activeConns > 0 && (
                  <div className="mt-1 text-xs">
                    {activeConns} active connection(s). Disconnect the agents
                    using this project, then retry.
                  </div>
                )}
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
            <Button
              type="submit"
              variant="destructive"
              disabled={submitting || !destructiveReady}
            >
              {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {deleteWorkspace ? "Remove + delete files" : "Remove"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
