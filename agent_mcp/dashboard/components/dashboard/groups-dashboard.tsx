"use client"

// Router-level group list / tree view — Phase 3 Wave 1b
// (prancy-napping-pie). Lives at the cross-project overview
// (``/agent-mcp/app/``). Groups can contain users OR other groups
// (nested membership); each row expands to show its current members
// with one-click remove + an add-member dialog.
//
// Backend: /agent-mcp/api/router/groups[/<id>][/members][/<member_id>]
//
// Shared-scaffold migration (architecture review, Classes 1/2/3/4):
// the page shell — header, first-load skeleton, "Sysadmin only" (403)
// panel, list-load error panel, empty state, desktop table + mobile
// card list — is owned by <DataTablePage>. The accordion survives via
// the scaffold's `renderExpanded` seam (a full-width colSpan row on
// desktop; inline inside the mobile card).
//
// Wave 5 extraction: this file is now a thin orchestrator. The column
// spec (`useGroupsColumns`), the create/edit/add-member modals (now on
// the shared `<FormDialog>`), the row detail panel and the capabilities
// checklist live in `components/dashboard/groups/` — mirroring the
// agents / messages / tasks resource subfolders. Only the page wiring,
// the accordion state, the delete flow (the shared <DeleteConfirmModal>,
// pinned here by the tier-3 confirm-word guards) and the
// desktop/mobile-half chooser stay here.
//
// Error surfacing: this file used to hand-roll ~19 `setError` sites
// AND a *reinvented* toast (a local `useState<string|null>` rendered
// as a green line, a same-name shadow of the real
// `@/components/ui/toast` module it never imported). Both are gone —
// every mutation now reports through the shared `toastError` /
// `toastSuccess`, and the list-load error is the scaffold's.

import React, { useCallback, useEffect, useMemo, useState } from "react"
import { Plus, Users as UsersIcon, Shield } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { toastError } from "@/components/ui/toast"
import { routerGroupUrl } from "@/lib/urls"
import { routerApi } from "@/lib/router-api"
import { useRouterQuery } from "@/hooks/use-router-query"
import { DeleteConfirmModal } from "./modals/delete-confirm-modal"
import { DataTablePage } from "@/components/dashboard/shared/data-table-page"
import { fetchGroups, memberLabel, type GroupRow } from "@/components/dashboard/groups/groups-api"
import { useGroupsColumns } from "@/components/dashboard/groups/use-groups-columns"
import { AddGroupModal } from "@/components/dashboard/groups/add-group-modal"
import { EditGroupModal } from "@/components/dashboard/groups/edit-group-modal"
import { GroupDetailPanel } from "@/components/dashboard/groups/group-detail-panel"

// <ResponsiveDataTable> renders BOTH halves of the responsive table
// (the desktop <table> and the mobile card list) and hides one with
// CSS. That is free for presentational cells — but the group detail
// panel FETCHES (members + capabilities) and owns state, so mounting
// it in both halves would double every request on expand. Host it in
// exactly one half, chosen with Tailwind's own `sm` breakpoint so the
// choice always matches the half that is actually visible.
const NARROW_VIEWPORT_QUERY = "(max-width: 639.98px)"

function useIsNarrowViewport(): boolean {
  // Defaults to false (= desktop table) so SSR / first paint agrees
  // with the table half the scaffold shows by default.
  const [narrow, setNarrow] = useState(false)
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return
    const mql = window.matchMedia(NARROW_VIEWPORT_QUERY)
    const onChange = () => setNarrow(mql.matches)
    onChange()
    mql.addEventListener("change", onChange)
    return () => mql.removeEventListener("change", onChange)
  }, [])
  return narrow
}


export function GroupsDashboard(): React.ReactElement {
  const {
    data,
    loading,
    error: fetchError,
    forbidden,
    refresh,
  } = useRouterQuery<GroupRow[]>(fetchGroups)
  const groups = useMemo(() => data ?? [], [data])
  const error = fetchError?.message ?? null
  const [addOpen, setAddOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<GroupRow | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<GroupRow | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const narrow = useIsNarrowViewport()

  const toggleExpand = useCallback((id: string) => {
    setExpanded((cur) => {
      const next = new Set(cur)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }, [])

  const handleDeleteGroup = useCallback(async (group: GroupRow) => {
    try {
      await routerApi.request(routerGroupUrl(group.group_id), {
        method: "DELETE",
      })
      await refresh()
    } catch (e) {
      // Toast for the page-level surface; re-throw so the shared
      // DeleteConfirmModal stays open with its inline error.
      toastError(e, "Failed to delete group")
      throw e
    }
  }, [refresh])

  // Column spec — one source drives the desktop table + the mobile
  // card's shared chevron/rowActions. Extracted to `useGroupsColumns`
  // (Wave 5, mirrors `useTasksColumns`).
  const { columns, chevron, rowActions } = useGroupsColumns({
    expanded,
    toggleExpand,
    onEdit: setEditTarget,
    onDelete: setDeleteTarget,
  })

  return (
    <DataTablePage<GroupRow>
      loading={loading}
      error={error}
      forbidden={forbidden}
      header={{
        title: "Groups",
        subtitle:
          "Group operators for bulk project access — supports nesting",
        onRefresh: refresh,
        refreshing: loading,
        actions: (
          <Button onClick={() => setAddOpen(true)} size="sm">
            <Plus className="h-4 w-4 mr-1" /> Add group
          </Button>
        ),
      }}
      // No stats strip on this page — keep the skeleton honest.
      skeletonStats={0}
      columns={columns}
      rows={groups}
      getRowId={(g) => g.group_id}
      onRowClick={(g) => toggleExpand(g.group_id)}
      renderExpanded={(group) =>
        !narrow && expanded.has(group.group_id) ? (
          <GroupDetailPanel group={group} onMembersChange={refresh} />
        ) : null
      }
      renderMobileCard={(group) => (
        <li className="px-4 py-3 space-y-2">
          <div className="flex items-start justify-between gap-2">
            <button
              type="button"
              onClick={() => toggleExpand(group.group_id)}
              aria-expanded={expanded.has(group.group_id)}
              aria-label={`${
                expanded.has(group.group_id) ? "Collapse" : "Expand"
              } ${group.name}`}
              className="flex items-center gap-2 text-left min-w-0"
            >
              {chevron(group)}
              <UsersIcon className="h-4 w-4 text-muted-foreground flex-shrink-0" />
              <span className="font-medium truncate">{group.name}</span>
              {group.is_sysadmin && (
                <Badge variant="default" className="flex items-center gap-1">
                  <Shield className="h-3 w-3" /> sysadmin
                </Badge>
              )}
            </button>
            {rowActions(group)}
          </div>
          <Badge variant="secondary" className="text-xs">
            {memberLabel(group.member_count)}
          </Badge>
          {narrow && expanded.has(group.group_id) && (
            <GroupDetailPanel group={group} onMembersChange={refresh} />
          )}
        </li>
      )}
      empty={{
        icon: UsersIcon,
        title: "No groups yet",
        description:
          "Groups bundle operators together for bulk project access.",
        action: (
          <Button onClick={() => setAddOpen(true)} size="sm">
            <Plus className="h-4 w-4 mr-1" /> Add group
          </Button>
        ),
      }}
    >
      <AddGroupModal
        open={addOpen}
        onOpenChange={setAddOpen}
        onCreated={refresh}
      />
      {editTarget && (
        <EditGroupModal
          group={editTarget}
          open={true}
          onOpenChange={(o) => !o && setEditTarget(null)}
          onSaved={refresh}
        />
      )}
      {/* Type-the-group-NAME-to-confirm delete (UX-08), now the shared
          <DeleteConfirmModal> with `requiredWord` + `matchCase`. */}
      <DeleteConfirmModal
        open={deleteTarget !== null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
        entityLabel="Group"
        title={deleteTarget ? `Delete ${deleteTarget.name}?` : "Delete group"}
        description="Removes the group and all its memberships. Members themselves are not deleted. This cannot be undone."
        warningText="This group and all of its memberships will be permanently removed. Members themselves are not deleted. This action cannot be reversed."
        requiredWord={deleteTarget?.name ?? ""}
        matchCase
        confirmLabel="Delete"
        inputId="delete-group-confirm"
        onConfirm={async () => {
          if (deleteTarget) await handleDeleteGroup(deleteTarget)
        }}
      />
    </DataTablePage>
  )
}
