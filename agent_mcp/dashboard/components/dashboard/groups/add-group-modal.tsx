"use client"

import { useState } from "react"
import { Plus } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { routerApi } from "@/lib/router-api"
import { routerGroupsUrl } from "@/lib/urls"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"

export interface AddGroupModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Refresh the list after a successful create. */
  onCreated: () => void | Promise<void>
}

/**
 * Add group — Wave 5: adopts the shared `<FormDialog>` + `useAsyncSubmit`
 * shell (mirrors tasks' create + messages' compose). The shell owns the
 * Dialog chrome, the Cancel/Create footer, the in-flight spinner and the
 * success/error toast + close-on-success; `onSubmit` posts, refreshes and
 * THROWS on failure so the shell keeps the dialog open with the draft
 * intact.
 */
export function AddGroupModal({ open, onOpenChange, onCreated }: AddGroupModalProps) {
  const [name, setName] = useState("")
  const [isSysadmin, setIsSysadmin] = useState(false)

  const reset = () => {
    setName("")
    setIsSysadmin(false)
  }

  const handleCreate = async () => {
    if (!name.trim()) return
    await routerApi.request(routerGroupsUrl(), {
      method: "POST",
      body: JSON.stringify({ name, is_sysadmin: isSysadmin }),
    })
    await onCreated()
    reset()
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Add group"
      description="Groups bundle operators together for bulk project access."
      icon={Plus}
      onSubmit={handleCreate}
      submitLabel="Create"
      submittingLabel="Creating…"
      submitDisabled={!name.trim()}
      successMessage="Group created."
      errorMessage="Failed to create group"
    >
      <div className="space-y-2">
        <Label htmlFor="add-group-name">Name</Label>
        <Input
          id="add-group-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={isSysadmin}
          onChange={(e) => setIsSysadmin(e.target.checked)}
        />
        Sysadmin group (members get sysadmin privileges)
      </label>
    </FormDialog>
  )
}
