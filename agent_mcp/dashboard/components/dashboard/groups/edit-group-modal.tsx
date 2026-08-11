"use client"

import { useEffect, useState } from "react"
import { Pencil } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { routerApi } from "@/lib/router-api"
import { routerGroupUrl } from "@/lib/urls"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import type { GroupRow } from "@/components/dashboard/groups/groups-api"

export interface EditGroupModalProps {
  group: GroupRow
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved: () => void | Promise<void>
}

/**
 * Edit group — Wave 5: adopts the shared `<FormDialog>` + `useAsyncSubmit`
 * shell (mirrors tasks' edit + the agents forms). The shell owns the
 * Dialog chrome + Cancel/Save footer + spinner + success/error toast +
 * close-on-success; `onSubmit` PATCHes, refreshes and THROWS on failure
 * so the dialog stays open with the operator's edits intact.
 */
export function EditGroupModal({ group, open, onOpenChange, onSaved }: EditGroupModalProps) {
  const [name, setName] = useState(group.name)
  const [isSysadmin, setIsSysadmin] = useState(group.is_sysadmin)

  // Re-seed whenever the dialog opens for a *different* group.
  const groupId = group.group_id
  useEffect(() => {
    setName(group.name)
    setIsSysadmin(group.is_sysadmin)
    // Depend on the group identity, not the whole object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId])

  const handleSave = async () => {
    await routerApi.request(routerGroupUrl(group.group_id), {
      method: "PATCH",
      body: JSON.stringify({ name, is_sysadmin: isSysadmin }),
    })
    await onSaved()
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Edit ${group.name}`}
      icon={Pencil}
      onSubmit={handleSave}
      submitLabel="Save"
      submittingLabel="Saving…"
      submitDisabled={!name.trim()}
      successMessage="Group updated."
      errorMessage="Failed to save group"
    >
      <div className="space-y-2">
        <Label htmlFor="edit-group-name">Name</Label>
        <Input
          id="edit-group-name"
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
        Sysadmin group
      </label>
    </FormDialog>
  )
}
