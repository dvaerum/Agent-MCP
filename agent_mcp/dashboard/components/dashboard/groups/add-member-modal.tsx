"use client"

import { useEffect, useState } from "react"
import { UserPlus } from "lucide-react"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { toastError } from "@/components/ui/toast"
import { routerApi } from "@/lib/router-api"
import { routerGroupMembersUrl } from "@/lib/urls"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import {
  fetchGroups,
  fetchUsers,
  type GroupRow,
  type UserRow,
} from "@/components/dashboard/groups/groups-api"

export interface AddMemberModalProps {
  groupId: string
  groupName: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onAdded: () => void | Promise<void>
}

/**
 * Add member — Wave 5: adopts the shared `<FormDialog>` + `useAsyncSubmit`
 * shell. Adds a single user or nests another group. The shell owns the
 * chrome + footer + spinner + toast + close-on-success; the Add button is
 * gated (`submitDisabled`) until an entity is picked, and `onSubmit`
 * POSTs the membership, refreshes and THROWS on failure.
 */
export function AddMemberModal({
  groupId,
  groupName,
  open,
  onOpenChange,
  onAdded,
}: AddMemberModalProps) {
  const [kind, setKind] = useState<"user" | "group">("user")
  const [users, setUsers] = useState<UserRow[]>([])
  const [groups, setGroups] = useState<GroupRow[]>([])
  const [selectedId, setSelectedId] = useState<string>("")
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    void Promise.all([fetchUsers(), fetchGroups()])
      .then(([us, gs]) => {
        setUsers(us)
        // Exclude self from the group list to prevent the obvious cycle —
        // deeper cycle detection is Wave 1a's job.
        setGroups(gs.filter((g) => g.group_id !== groupId))
      })
      .catch((e) => toastError(e, "Failed to load members to add"))
      .finally(() => setLoading(false))
  }, [open, groupId])

  const handleAdd = async () => {
    if (!selectedId) return
    const body =
      kind === "user" ? { user_id: selectedId } : { group_id: selectedId }
    await routerApi.request(routerGroupMembersUrl(groupId), {
      method: "POST",
      body: JSON.stringify(body),
    })
    await onAdded()
    setSelectedId("")
  }

  const options = kind === "user" ? users : groups

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Add member to ${groupName}`}
      description="Add a single user or nest another group."
      icon={UserPlus}
      onSubmit={handleAdd}
      submitLabel="Add"
      submittingLabel="Adding…"
      submitDisabled={!selectedId}
      successMessage="Member added."
      errorMessage="Failed to add member"
    >
      <div className="space-y-2">
        <Label htmlFor="add-member-kind">Kind</Label>
        <Select
          value={kind}
          onValueChange={(v) => {
            setKind(v as "user" | "group")
            setSelectedId("")
          }}
        >
          <SelectTrigger id="add-member-kind">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="user">User</SelectItem>
            <SelectItem value="group">Group</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-2">
        <Label htmlFor="add-member-entity">{kind === "user" ? "User" : "Group"}</Label>
        {loading ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : (
          <Select value={selectedId} onValueChange={setSelectedId}>
            <SelectTrigger id="add-member-entity">
              <SelectValue placeholder={`Select a ${kind}`} />
            </SelectTrigger>
            <SelectContent>
              {options.length === 0 && (
                <SelectItem disabled value="__none__">
                  None available
                </SelectItem>
              )}
              {kind === "user"
                ? (options as UserRow[]).map((u) => (
                    <SelectItem key={u.user_id} value={u.user_id}>
                      {u.username}
                    </SelectItem>
                  ))
                : (options as GroupRow[]).map((g) => (
                    <SelectItem key={g.group_id} value={g.group_id}>
                      {g.name}
                    </SelectItem>
                  ))}
            </SelectContent>
          </Select>
        )}
      </div>
    </FormDialog>
  )
}
