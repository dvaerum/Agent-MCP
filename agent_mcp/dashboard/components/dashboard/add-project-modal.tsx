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

import { useState } from "react"
import type { ReactElement } from "react"
import { Plus } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import { useProjectsStore } from "@/lib/stores/projects-store"
import { routerProjectsUrl } from "@/lib/urls"
import { routerApi } from "@/lib/router-api"

const SLUG_RE = /^[a-z](?:[a-z0-9-]*[a-z0-9])?$/

export interface AddProjectModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Add project — adopts the shared <FormDialog> shell (mirrors
 * EditTaskDialog/AddGroupModal): the shell owns the mobile dvh-cap +
 * scroll body, the Cancel/Create footer, the in-flight spinner and the
 * success/error toast. `onSubmit` builds the request, mutates, and
 * THROWS on failure so the shell keeps the dialog open with the
 * operator's input intact (was previously an inline error paragraph;
 * now a toast, matching every other FormDialog adopter).
 */
export function AddProjectModal({
  open,
  onOpenChange,
}: AddProjectModalProps): ReactElement {
  const [name, setName] = useState("")
  const [workspace, setWorkspace] = useState("")
  const fetchOverview = useProjectsStore((s) => s.fetchOverview)

  const validName = SLUG_RE.test(name)

  const handleSubmit = async () => {
    await routerApi.request(routerProjectsUrl(), {
      method: "POST",
      body: JSON.stringify({ name }),
    })
    await fetchOverview()
  }

  const reset = () => {
    setName("")
    setWorkspace("")
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o)
        if (!o) reset()
      }}
      title="Add a new project"
      description="Register a new agent-mcp project on this router. Leave the workspace blank to let the router create one under the default location."
      icon={Plus}
      onSubmit={handleSubmit}
      submitLabel="Create"
      submitDisabled={!validName}
      successMessage="Project created."
      errorMessage="Failed to add project"
    >
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
          Editable for the &quot;restore from existing folder&quot; use case.
        </p>
      </div>
    </FormDialog>
  )
}
