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
//   * sends ``?delete_workspace=true`` on the DELETE to opt in.
//
// Refuse path: router returns 409 with
// ``{error: "active_sessions", active_connections: N}`` when the
// project has any in-flight MCP/REST traffic. We surface the count
// and ask the operator to disconnect agents before retrying.

import React, { useState } from "react"
import { AlertTriangle, Loader2, Trash2 } from "lucide-react"
import { routerProjectUrl } from "@/lib/urls"
import { routerApi } from "@/lib/router-api"
import { ApiError } from "@/lib/api"
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
      // ADR 0014: DELETE /api/router/projects/<name>. Cascade signal
      // is a query-string flag (?delete_workspace=true) not a body
      // field, because browsers strip DELETE bodies on some Fetch
      // implementations (audit §3.2).
      const url = deleteWorkspace
        ? routerProjectUrl(projectName, "delete_workspace=true")
        : routerProjectUrl(projectName)
      await routerApi.request(url, { method: "DELETE" })
      await fetchOverview()
      close()
    } catch (err) {
      // 409 active_sessions: the router refuses removal while agents are
      // connected. Re-parse the ApiError body to surface the connection
      // count instead of a generic error string.
      if (err instanceof ApiError && err.status === 409) {
        let body: {
          error?: string
          active_connections?: number
          message?: string
        } = {}
        try {
          body = JSON.parse(err.body)
        } catch {
          /* non-JSON body — fall through to the generic handler */
        }
        if (body.error === "active_sessions") {
          setActiveConns(
            typeof body.active_connections === "number"
              ? body.active_connections
              : 0,
          )
          setError(body.message || "Active sessions block removal.")
          setSubmitting(false)
          return
        }
      }
      setError(err instanceof Error ? err.message : String(err))
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? onOpenChange(true) : close())}>
      <DialogContent alertDialog className="w-[calc(100vw-2rem)] flex max-h-[calc(100dvh-2rem)] flex-col overflow-hidden sm:!max-w-lg">
        <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col">
          <DialogHeader className="flex-shrink-0">
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

          <div className="flex-1 min-h-0 space-y-4 overflow-y-auto py-4 pr-1">
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
                  Recursively removes the project&apos;s workspace directory.
                  Only allowed when the workspace lives under the
                  router&apos;s default workspace parent.
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

          <DialogFooter className="flex-shrink-0">
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
