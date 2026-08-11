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
// Error surfacing: this file used to hand-roll ~19 `setError` sites
// AND a *reinvented* toast (a local `useState<string|null>` rendered
// as a green line, a same-name shadow of the real
// `@/components/ui/toast` module it never imported). Both are gone —
// every mutation now reports through the shared `toastError` /
// `toastSuccess`, and the list-load error is the scaffold's.

import React, { useCallback, useEffect, useMemo, useState } from "react"
import {
  Loader2, Plus, Pencil, Trash2, ChevronDown, ChevronRight,
  Shield, User as UserIcon, Users as UsersIcon, X,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { toastError, toastSuccess, toastUndo } from "@/components/ui/toast"
import {
  routerGroupsUrl, routerGroupUrl,
  routerGroupMembersUrl, routerGroupMemberUrl,
  routerGroupCapabilitiesUrl,
  routerUsersUrl,
} from "@/lib/urls"
import {
  CAPABILITY_DESCRIPTIONS,
  CAPABILITY_RESOURCE_LABELS,
  groupCapabilitiesByResource,
} from "@/lib/capability-descriptions"
import { routerApi } from "@/lib/router-api"
import { ApiError } from "@/lib/api"
import { useRouterQuery } from "@/hooks/use-router-query"
import { DeleteConfirmModal } from "./modals/delete-confirm-modal"
import { DataTablePage } from "@/components/dashboard/shared/data-table-page"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"

interface GroupRow {
  group_id: string
  name: string
  is_sysadmin: boolean
  created_at: string
  member_count: number
}

interface MemberRow {
  user_id?: string
  username?: string
  group_id?: string
  name?: string
  member_group_is_sysadmin?: boolean
  added_at: string
}

interface UserRow {
  user_id: string
  username: string
  email: string | null
  is_sysadmin: boolean
}

async function fetchGroups(signal?: AbortSignal): Promise<GroupRow[]> {
  const body = await routerApi.request<{ groups?: GroupRow[] }>(
    routerGroupsUrl(),
    signal ? { signal } : {},
  )
  return body.groups || []
}

async function fetchMembers(groupId: string): Promise<MemberRow[]> {
  const body = await routerApi.request<{ members?: MemberRow[] }>(
    routerGroupMembersUrl(groupId),
  )
  return body.members || []
}

async function fetchUsers(): Promise<UserRow[]> {
  const body = await routerApi.request<{ users?: UserRow[] }>(
    routerUsersUrl(),
  )
  return body.users || []
}

const memberLabel = (n: number) => `${n} member${n === 1 ? "" : "s"}`

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

  const chevron = (group: GroupRow) =>
    expanded.has(group.group_id) ? (
      <ChevronDown className="h-4 w-4" />
    ) : (
      <ChevronRight className="h-4 w-4" />
    )

  const rowActions = (group: GroupRow) => (
    <div className="flex items-center gap-1">
      <Button
        variant="ghost"
        size="icon"
        aria-label={`Edit ${group.name}`}
        onClick={(e) => {
          e.stopPropagation()
          setEditTarget(group)
        }}
      >
        <Pencil className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        aria-label={`Delete ${group.name}`}
        className="text-destructive"
        onClick={(e) => {
          e.stopPropagation()
          setDeleteTarget(group)
        }}
      >
        <Trash2 className="h-4 w-4" />
      </Button>
    </div>
  )

  // Desktop column spec. Row-body click toggles the accordion; every
  // action stopPropagation's so it doesn't also expand/collapse. The
  // mobile half is a bespoke card (`renderMobileCard` below) because
  // the summary reflows into two lines rather than stacking cells.
  const columns: Column<GroupRow>[] = [
    {
      id: "expand",
      header: <span className="sr-only">Expand</span>,
      headClassName: "w-10",
      cellClassName: "w-10",
      cell: (group) => (
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          aria-label={`${
            expanded.has(group.group_id) ? "Collapse" : "Expand"
          } ${group.name}`}
          aria-expanded={expanded.has(group.group_id)}
          onClick={(e) => {
            e.stopPropagation()
            toggleExpand(group.group_id)
          }}
        >
          {chevron(group)}
        </Button>
      ),
    },
    {
      id: "name",
      header: "Group",
      cell: (group) => (
        <div className="flex items-center gap-2 min-w-0">
          <UsersIcon className="h-4 w-4 text-muted-foreground flex-shrink-0" />
          <span className="font-medium truncate">{group.name}</span>
          {group.is_sysadmin && (
            <Badge variant="default" className="flex items-center gap-1">
              <Shield className="h-3 w-3" /> sysadmin
            </Badge>
          )}
        </div>
      ),
    },
    {
      id: "members",
      header: "Members",
      cell: (group) => (
        <Badge variant="secondary" className="text-xs">
          {memberLabel(group.member_count)}
        </Badge>
      ),
    },
    {
      id: "actions",
      header: "Actions",
      headClassName: "w-24",
      cell: rowActions,
    },
  ]

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


// ── Row detail: members + capabilities ──────────────────────────────


function GroupDetailPanel({
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

  // The panel only mounts while its row is expanded, so mount ==
  // "the operator just opened this group".
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


// ── Capabilities (Wave 9 PR 5) ──────────────────────────────────────


function GroupCapabilitiesSection({
  groupId,
  groupName,
}: {
  groupId: string
  groupName: string
}): React.ReactElement {
  // Three load states:
  //   * ``loaded``    GET succeeded → render the checklist.
  //   * ``forbidden`` GET returned 403 → we are not sysadmin; show
  //                   the read-only / disabled message (plan: "show
  //                   but don't allow edit; tooltip 'requires sysadmin'").
  //   * ``loading`` — transient banner. Errors go to the shared toast.
  //
  // ``loaded`` / ``selected`` / ``forbidden`` stay LOCAL state (not
  // hook-owned) rather than reading straight off ``useRouterQuery``'s
  // ``data`` — ``save()`` below writes an OPTIMISTIC result into them
  // straight from the PUT response (no extra GET round-trip), and the
  // checklist's dirty-tracking (``selected``) needs to be
  // independently editable. The hook still owns the GET's own
  // loading/error/forbidden bookkeeping; a sync effect below folds its
  // outcome into the local state exactly the way the old inline
  // ``load()`` used to.
  const [loaded, setLoaded] = React.useState<string[] | null>(null)
  const [selected, setSelected] = React.useState<Set<string>>(new Set())
  const [forbidden, setForbidden] = React.useState(false)
  const [saving, setSaving] = React.useState(false)

  const {
    loading,
    data: fetchedCaps,
    error: loadError,
    forbidden: loadForbidden,
  } = useRouterQuery<string[]>(
    useCallback(async (signal) => {
      const body = await routerApi.request<{ capabilities?: string[] }>(
        routerGroupCapabilitiesUrl(groupId),
        { signal },
      )
      return body.capabilities ?? []
    }, [groupId]),
    { deps: [groupId] },
  )

  // Mirrors the old ``load()``'s synchronous reset — as soon as a
  // fetch starts, any stale forbidden from a previous attempt is
  // cleared.
  useEffect(() => {
    if (loading) {
      setForbidden(false)
    }
  }, [loading])

  useEffect(() => {
    if (fetchedCaps !== null) {
      setLoaded(fetchedCaps)
      setSelected(new Set(fetchedCaps))
    }
  }, [fetchedCaps])

  useEffect(() => {
    // 403 = not sysadmin: render the read-only message, not an error.
    if (loadForbidden) {
      setForbidden(true)
      setLoaded([])
      setSelected(new Set())
    }
  }, [loadForbidden])

  useEffect(() => {
    if (loadError) {
      toastError(loadError, "Failed to load capabilities")
    }
  }, [loadError])

  const dirty = React.useMemo(() => {
    if (loaded === null) return false
    if (loaded.length !== selected.size) return true
    for (const cap of loaded) {
      if (!selected.has(cap)) return true
    }
    return false
  }, [loaded, selected])

  const toggleCap = (cap: string) => {
    setSelected((cur) => {
      const next = new Set(cur)
      if (next.has(cap)) next.delete(cap)
      else next.add(cap)
      return next
    })
  }

  const cancel = () => {
    if (loaded !== null) {
      setSelected(new Set(loaded))
    }
  }

  const save = async () => {
    setSaving(true)
    try {
      const body = await routerApi.request<{
        success: true
        capabilities: string[]
      }>(routerGroupCapabilitiesUrl(groupId), {
        method: "PUT",
        body: JSON.stringify({ capabilities: [...selected] }),
      })
      const newCaps = body.capabilities ?? []
      setLoaded(newCaps)
      setSelected(new Set(newCaps))
      // Was the page's REINVENTED toast (a local `setToast` + green
      // <div>); same message, now the shared toast module.
      toastSuccess(
        `Saved — ${groupName} now has ${newCaps.length} capabilit${
          newCaps.length === 1 ? "y" : "ies"
        }`,
      )
    } catch (e) {
      // 403 = not sysadmin: flag forbidden + a specific hint.
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true)
        toastError(
          "requires sysadmin — group capabilities are sysadmin-only",
          "Failed to save capabilities",
        )
      } else {
        toastError(e, "Failed to save capabilities")
      }
    } finally {
      setSaving(false)
    }
  }

  const grouped = React.useMemo(() => {
    // Render every KNOWN cap (sourced from the description registry —
    // the build-time test ``capability-descriptions-complete`` keeps
    // it in lockstep with ``core/capabilities.py::KNOWN_CAPABILITIES``).
    // We bucket by resource so the UI matches the mental model
    // operators have when reading the bundle table.
    const allKnown = Object.keys(CAPABILITY_DESCRIPTIONS)
    return groupCapabilitiesByResource(allKnown)
  }, [])

  return (
    <div className="border-t pt-3 mt-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-xs font-semibold text-muted-foreground uppercase flex items-center gap-2">
          <Shield className="h-3 w-3" /> Capabilities
        </div>
        {dirty && !forbidden && (
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              onClick={cancel}
              disabled={saving}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={save}
              disabled={saving}
            >
              {saving && <Loader2 className="h-3 w-3 mr-1 animate-spin" />}
              Save
            </Button>
          </div>
        )}
      </div>
      <p className="text-xs text-muted-foreground italic">
        Capabilities here are added on top of what each user&apos;s project
        role already grants. To remove a baseline capability, change
        PROJECT_ROLE_BUNDLES in source.
      </p>
      {loading && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="h-3 w-3 animate-spin" /> Loading capabilities…
        </div>
      )}
      {forbidden && !loading && (
        <div
          className="text-sm text-muted-foreground bg-muted/40 rounded px-2 py-1"
          title="requires sysadmin"
        >
          Requires sysadmin to view or edit capabilities for this group.
        </div>
      )}
      {!loading && !forbidden && (
        <div className="space-y-1">
          {grouped.map(({ resource, caps }) => (
            <CapabilityResourceSection
              key={resource}
              resource={resource}
              caps={caps}
              selected={selected}
              disabled={saving}
              onToggle={toggleCap}
            />
          ))}
        </div>
      )}
    </div>
  )
}


function CapabilityResourceSection({
  resource,
  caps,
  selected,
  disabled,
  onToggle,
}: {
  resource: string
  caps: string[]
  selected: Set<string>
  disabled: boolean
  onToggle: (cap: string) => void
}): React.ReactElement {
  const [open, setOpen] = useState(true)
  const label = CAPABILITY_RESOURCE_LABELS[resource] ?? resource
  const onCount = caps.filter((c) => selected.has(c)).length
  return (
    <div className="border rounded bg-background">
      <button
        type="button"
        className="w-full flex items-center justify-between px-2 py-1 text-left text-sm"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="flex items-center gap-1 font-medium">
          {open ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
          {label}
        </span>
        <Badge variant="secondary" className="text-xs">
          {onCount} / {caps.length}
        </Badge>
      </button>
      {open && (
        <div className="px-3 py-2 space-y-1 border-t">
          {caps.map((cap) => (
            <label
              key={cap}
              className="flex items-start gap-2 text-xs cursor-pointer"
              title={CAPABILITY_DESCRIPTIONS[cap]}
            >
              <input
                type="checkbox"
                checked={selected.has(cap)}
                disabled={disabled}
                onChange={() => onToggle(cap)}
                className="mt-0.5"
              />
              <span className="flex-1">
                <code className="font-mono text-[11px] mr-1">{cap}</code>
                <span className="text-muted-foreground">
                  {CAPABILITY_DESCRIPTIONS[cap]}
                </span>
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}


function AddGroupModal({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: () => void | Promise<void>
}): React.ReactElement {
  const [name, setName] = useState("")
  const [isSysadmin, setIsSysadmin] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const reset = () => {
    setName("")
    setIsSysadmin(false)
    setSubmitting(false)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      await routerApi.request(routerGroupsUrl(), {
        method: "POST",
        body: JSON.stringify({ name, is_sysadmin: isSysadmin }),
      })
      await onCreated()
      reset()
      onOpenChange(false)
    } catch (e) {
      toastError(e, "Failed to create group")
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) reset()
        onOpenChange(o)
      }}
    >
      <DialogContent className="w-[calc(100vw-2rem)] sm:max-w-lg">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Add group</DialogTitle>
            <DialogDescription>
              Groups bundle operators together for bulk project access.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
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
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                reset()
                onOpenChange(false)
              }}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting}>
              {submitting && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}


function EditGroupModal({
  group,
  open,
  onOpenChange,
  onSaved,
}: {
  group: GroupRow
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved: () => void | Promise<void>
}): React.ReactElement {
  const [name, setName] = useState(group.name)
  const [isSysadmin, setIsSysadmin] = useState(group.is_sysadmin)
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      await routerApi.request(routerGroupUrl(group.group_id), {
        method: "PATCH",
        body: JSON.stringify({ name, is_sysadmin: isSysadmin }),
      })
      await onSaved()
      onOpenChange(false)
    } catch (e) {
      toastError(e, "Failed to save group")
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:max-w-lg">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Edit {group.name}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-4">
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
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting}>
              {submitting && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}


function AddMemberModal({
  groupId,
  groupName,
  open,
  onOpenChange,
  onAdded,
}: {
  groupId: string
  groupName: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onAdded: () => void | Promise<void>
}): React.ReactElement {
  const [kind, setKind] = useState<"user" | "group">("user")
  const [users, setUsers] = useState<UserRow[]>([])
  const [groups, setGroups] = useState<GroupRow[]>([])
  const [selectedId, setSelectedId] = useState<string>("")
  const [submitting, setSubmitting] = useState(false)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    void Promise.all([fetchUsers(), fetchGroups()])
      .then(([us, gs]) => {
        setUsers(us)
        // Exclude self from group list to prevent the obvious cycle —
        // deeper cycle detection is Wave 1a's job.
        setGroups(gs.filter((g) => g.group_id !== groupId))
      })
      .catch((e) => toastError(e, "Failed to load members to add"))
      .finally(() => setLoading(false))
  }, [open, groupId])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedId) {
      toastError("Pick a user or group to add", "Nothing selected")
      return
    }
    setSubmitting(true)
    try {
      const body =
        kind === "user"
          ? { user_id: selectedId }
          : { group_id: selectedId }
      await routerApi.request(routerGroupMembersUrl(groupId), {
        method: "POST",
        body: JSON.stringify(body),
      })
      await onAdded()
      setSelectedId("")
      onOpenChange(false)
    } catch (e) {
      toastError(e, "Failed to add member")
    } finally {
      setSubmitting(false)
    }
  }

  const options = kind === "user" ? users : groups

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:max-w-lg">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Add member to {groupName}</DialogTitle>
            <DialogDescription>
              Add a single user or nest another group.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
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
                <Select
                  value={selectedId}
                  onValueChange={setSelectedId}
                >
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
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting || !selectedId}>
              {submitting && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
              Add
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
