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

import { useState } from "react"
import type { ReactElement } from "react"
import { AlertTriangle, Trash2 } from "lucide-react"
import { routerProjectUrl } from "@/lib/urls"
import { routerApi } from "@/lib/router-api"
import { ApiError } from "@/lib/api"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import { useProjectsStore } from "@/lib/stores/projects-store"

export interface RemoveProjectModalProps {
  projectName: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Remove project — adopts the shared <FormDialog> shell with
 * `alertDialog` (this is a destructive, `role="alertdialog"` confirm
 * form, not a plain create/edit one). `onSubmit` DELETEs the
 * router-admin REST resource (ADR 0014) and THROWS on failure so the
 * shell keeps the dialog open with the operator's checkbox/confirm-name
 * state intact + surfaces the toast. The 409 active_sessions refusal
 * doesn't fit FormDialog's generic string errorMessage, so it's
 * folded into one thrown Error message carrying both the router's
 * reason and the connection count.
 */
export function RemoveProjectModal({
  projectName,
  open,
  onOpenChange,
}: RemoveProjectModalProps): ReactElement {
  const [deleteWorkspace, setDeleteWorkspace] = useState(false)
  const [confirmName, setConfirmName] = useState("")
  const fetchOverview = useProjectsStore((s) => s.fetchOverview)

  const reset = () => {
    setDeleteWorkspace(false)
    setConfirmName("")
  }

  const destructiveReady =
    !deleteWorkspace || confirmName === projectName

  const handleSubmit = async () => {
    // ADR 0014: DELETE /api/router/projects/<name>. Cascade signal is
    // a query-string flag (?delete_workspace=true) not a body field,
    // because browsers strip DELETE bodies on some Fetch
    // implementations (audit §3.2).
    const url = deleteWorkspace
      ? routerProjectUrl(projectName, "delete_workspace=true")
      : routerProjectUrl(projectName)
    try {
      await routerApi.request(url, { method: "DELETE" })
    } catch (err) {
      // 409 active_sessions: the router refuses removal while agents
      // are connected. Re-parse the ApiError body to surface the
      // connection count instead of a generic error string — folded
      // into one message since the shell's toast only carries one.
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
          const count =
            typeof body.active_connections === "number"
              ? body.active_connections
              : 0
          throw new Error(
            `${body.message || "Active sessions block removal."} ` +
              `${count} active connection(s) — disconnect the agents ` +
              "using this project, then retry.",
          )
        }
      }
      throw err
    }
    await fetchOverview()
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o)
        if (!o) reset()
      }}
      alertDialog
      title={
        <span className="flex items-center gap-2">
          <Trash2 className="h-4 w-4" />
          Remove project <code className="text-base">{projectName}</code>
        </span>
      }
      description="Stops the systemd backend and drops the project from the router registry. Workspace files are kept by default; opt in below if you want them wiped too."
      onSubmit={handleSubmit}
      submitLabel={deleteWorkspace ? "Remove + delete files" : "Remove"}
      submitVariant="destructive"
      submitDisabled={!destructiveReady}
      successMessage={
        deleteWorkspace ? "Project and workspace removed." : "Project removed."
      }
      errorMessage="Failed to remove project"
    >
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
            Only allowed when the workspace lives under the router&apos;s
            default workspace parent.
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
    </FormDialog>
  )
}
