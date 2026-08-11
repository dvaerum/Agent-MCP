"use client"

import React, { useCallback, useEffect, useState } from "react"
import {
  Loader2, Plus, User as UserIcon, Users as UsersIcon, X,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { toastError, toastUndo } from "@/components/ui/toast"
import { routerApi } from "@/lib/router-api"
import { routerGroupMembersUrl, routerGroupMemberUrl } from "@/lib/urls"
import {
  fetchMembers,
  type GroupRow,
  type MemberRow,
} from "@/components/dashboard/groups/groups-api"
import { AddMemberModal } from "@/components/dashboard/groups/add-member-modal"
import { GroupCapabilitiesSection } from "@/components/dashboard/groups/group-capabilities-section"

// Row detail: members + capabilities. Extracted in Wave 5 into the
// groups/ subfolder. The panel only mounts while its row is expanded, so
// mount == "the operator just opened this group".
export function GroupDetailPanel({
  group,
  onMembersChange,
}: {
  group: GroupRow
  onMembersChange: () => void | Promise<void>
}): React.ReactElement {
  const [members, setMembers] = useState<MemberRow[] | null>(null)
  const [loadingMembers, setLoadingMembers] = useState(false)
  const [addMemberOpen, setAddMemberOpen] = useState(false)

  const refreshMembers = useCallback(async () => {
    setLoadingMembers(true)
    try {
      setMembers(await fetchMembers(group.group_id))
    } catch (e) {
      toastError(e, "Failed to load members")
    } finally {
      setLoadingMembers(false)
    }
  }, [group.group_id])

  useEffect(() => {
    void refreshMembers()
  }, [refreshMembers])

  // Removing a member deletes exactly ONE `group_membership` row and
  // the inverse is the very POST the Add-member modal already makes —
  // so this stays a no-dialog action and pays for that by OFFERING the
  // reversal instead (Material: "confirmation isn't necessary when the
  // consequences of an action are reversible"). Pre-PR it succeeded in
  // total silence.
  const handleRemoveMember = async (m: MemberRow) => {
    const memberId = m.user_id ?? m.group_id!
    const label = m.username || m.name || memberId
    const body = m.user_id ? { user_id: m.user_id } : { group_id: m.group_id }
    try {
      await routerApi.request(
        routerGroupMemberUrl(group.group_id, memberId),
        { method: "DELETE" },
      )
      await refreshMembers()
      await onMembersChange()
      toastUndo(
        `Removed ${label} from ${group.name}.`,
        async () => {
          await routerApi.request(routerGroupMembersUrl(group.group_id), {
            method: "POST",
            body: JSON.stringify(body),
          })
          await refreshMembers()
          await onMembersChange()
        },
        { undoneMessage: `${label} restored to ${group.name}.` },
      )
    } catch (e) {
      toastError(e, "Failed to remove member")
    }
  }

  return (
    <div className="border-t px-4 py-3 space-y-2 bg-muted/30">
      <div className="flex items-center justify-between">
        <div className="text-xs font-semibold text-muted-foreground uppercase">
          Members
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setAddMemberOpen(true)}
        >
          <Plus className="h-3 w-3 mr-1" /> Add member
        </Button>
      </div>
      {loadingMembers && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="h-3 w-3 animate-spin" /> Loading…
        </div>
      )}
      {!loadingMembers && members && members.length === 0 && (
        <div className="text-sm text-muted-foreground">
          No members yet.
        </div>
      )}
      {!loadingMembers && members?.map((m) => {
        const id = m.user_id ?? m.group_id!
        const isUser = !!m.user_id
        return (
          <div
            key={`${isUser ? "u" : "g"}:${id}`}
            className="flex items-center justify-between bg-background rounded px-2 py-1"
          >
            <div className="flex items-center gap-2 text-sm">
              {isUser ? (
                <UserIcon className="h-3 w-3 text-muted-foreground" />
              ) : (
                <UsersIcon className="h-3 w-3 text-muted-foreground" />
              )}
              <span>{m.username || m.name}</span>
              {!isUser && m.member_group_is_sysadmin && (
                <Badge variant="default" className="text-xs">
                  sysadmin
                </Badge>
              )}
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6"
              aria-label={`Remove ${m.username || m.name}`}
              onClick={() => handleRemoveMember(m)}
            >
              <X className="h-3 w-3" />
            </Button>
          </div>
        )
      })}
      <AddMemberModal
        groupId={group.group_id}
        groupName={group.name}
        open={addMemberOpen}
        onOpenChange={setAddMemberOpen}
        onAdded={async () => {
          await refreshMembers()
          await onMembersChange()
        }}
      />
      <GroupCapabilitiesSection
        groupId={group.group_id}
        groupName={group.name}
      />
    </div>
  )
}
