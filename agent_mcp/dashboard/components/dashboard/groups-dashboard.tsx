"use client"

// Router-level group list / tree view — Phase 3 Wave 1b
// (prancy-napping-pie). Lives at the cross-project overview
// (``/agent-mcp/app/``). Groups can contain users OR other groups
// (nested membership); each row expands to show its current members
// with one-click remove + an add-member dialog.
//
// Backend: /agent-mcp/api/router/groups[/<id>][/members][/<member_id>]

import React, { useCallback, useEffect, useState } from "react"
import {
  Loader2, Plus, Pencil, Trash2, ChevronDown, ChevronRight,
  Shield, ShieldAlert, User as UserIcon, Users as UsersIcon, X,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
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


export function GroupsDashboard(): React.ReactElement {
  const {
    data,
    loading,
    error: fetchError,
    forbidden,
    refresh,
  } = useRouterQuery<GroupRow[]>(fetchGroups)
  const groups = data ?? []
  const error = fetchError?.message ?? null
  const [addOpen, setAddOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<GroupRow | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<GroupRow | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggleExpand = (id: string) => {
    setExpanded((cur) => {
      const next = new Set(cur)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  return (
    <div className="flex flex-col h-full w-full">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between px-[var(--space-fluid-lg)] py-[var(--space-fluid-md)] gap-[var(--space-fluid-sm)] border-b bg-background/95">
        <div>
          <h1 className="text-fluid-2xl font-bold tracking-tight">Groups</h1>
          <p className="text-fluid-base text-muted-foreground mt-1">
            Group operators for bulk project access — supports nesting
          </p>
        </div>
        <Button onClick={() => setAddOpen(true)} size="sm">
          <Plus className="h-4 w-4 mr-1" /> Add group
        </Button>
      </div>

      <div className="flex-1 overflow-auto p-[var(--space-fluid-lg)] space-y-2">
        {loading && (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading groups…
          </div>
        )}
        {!loading && forbidden && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldAlert className="h-4 w-4 text-destructive" />
                Sysadmin only
              </CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              You don&apos;t have sysadmin privileges. Ask a sysadmin to view
              or manage groups on your behalf.
            </CardContent>
          </Card>
        )}
        {!loading && !forbidden && error && (
          <div className="text-destructive text-sm">Error: {error}</div>
        )}
        {!loading && !forbidden && !error && groups.length === 0 && (
          <div className="text-muted-foreground text-center py-8">
            No groups yet.
          </div>
        )}
        {!loading && !forbidden && !error && groups.map((g) => (
          <GroupCard
            key={g.group_id}
            group={g}
            expanded={expanded.has(g.group_id)}
            onToggle={() => toggleExpand(g.group_id)}
            onEdit={() => setEditTarget(g)}
            onDelete={() => setDeleteTarget(g)}
            onMembersChange={refresh}
          />
        ))}
      </div>

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
      {deleteTarget && (
        <DeleteGroupModal
          group={deleteTarget}
          open={true}
          onOpenChange={(o) => !o && setDeleteTarget(null)}
          onDeleted={refresh}
        />
      )}
    </div>
  )
}


function GroupCard({
  group,
  expanded,
  onToggle,
  onEdit,
  onDelete,
  onMembersChange,
}: {
  group: GroupRow
  expanded: boolean
  onToggle: () => void
  onEdit: () => void
  onDelete: () => void
  onMembersChange: () => void | Promise<void>
}): React.ReactElement {
  const [members, setMembers] = useState<MemberRow[] | null>(null)
  const [loadingMembers, setLoadingMembers] = useState(false)
  const [addMemberOpen, setAddMemberOpen] = useState(false)
  const [memberError, setMemberError] = useState<string | null>(null)

  const refreshMembers = useCallback(async () => {
    setLoadingMembers(true)
    setMemberError(null)
    try {
      setMembers(await fetchMembers(group.group_id))
    } catch (e) {
      setMemberError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingMembers(false)
    }
  }, [group.group_id])

  useEffect(() => {
    if (expanded && members === null) {
      void refreshMembers()
    }
  }, [expanded, members, refreshMembers])

  const handleRemoveMember = async (memberId: string) => {
    try {
      await routerApi.request(
        routerGroupMemberUrl(group.group_id, memberId),
        { method: "DELETE" },
      )
      await refreshMembers()
      await onMembersChange()
    } catch (e) {
      setMemberError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="border rounded-md bg-card">
      <div className="flex items-center justify-between px-4 py-3">
        <button
          type="button"
          onClick={onToggle}
          className="flex items-center gap-2 text-left"
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
          <UsersIcon className="h-4 w-4 text-muted-foreground" />
          <span className="font-medium">{group.name}</span>
          {group.is_sysadmin && (
            <Badge variant="default" className="flex items-center gap-1">
              <Shield className="h-3 w-3" /> sysadmin
            </Badge>
          )}
          <Badge variant="secondary" className="text-xs">
            {group.member_count} member{group.member_count === 1 ? "" : "s"}
          </Badge>
        </button>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Edit ${group.name}`}
            onClick={onEdit}
          >
            <Pencil className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Delete ${group.name}`}
            className="text-destructive"
            onClick={onDelete}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>
      {expanded && (
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
          {memberError && (
            <div className="text-destructive text-sm">{memberError}</div>
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
                  onClick={() => handleRemoveMember(id)}
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
      )}
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
  //   * ``loading`` / ``error`` — transient banners.
  //
  // ``loaded`` / ``selected`` / ``forbidden`` / ``error`` stay LOCAL
  // state (not hook-owned) rather than reading straight off
  // ``useRouterQuery``'s ``data`` — ``save()`` below writes an
  // OPTIMISTIC result into them straight from the PUT response (no
  // extra GET round-trip), and the checklist's dirty-tracking
  // (``selected``) needs to be independently editable. The hook still
  // owns the GET's own loading/error/forbidden bookkeeping; a sync
  // effect below folds its outcome into the local state exactly the
  // way the old inline ``load()`` used to.
  const [loaded, setLoaded] = React.useState<string[] | null>(null)
  const [selected, setSelected] = React.useState<Set<string>>(new Set())
  const [forbidden, setForbidden] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [saving, setSaving] = React.useState(false)
  const [toast, setToast] = React.useState<string | null>(null)

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
  // fetch starts, any stale forbidden/error/toast from a previous
  // attempt is cleared.
  useEffect(() => {
    if (loading) {
      setForbidden(false)
      setError(null)
      setToast(null)
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
      setError(loadError.message)
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
    setError(null)
    setToast(null)
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    setToast(null)
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
      setToast(
        `Saved — ${groupName} now has ${newCaps.length} capabilit${
          newCaps.length === 1 ? "y" : "ies"
        }`,
      )
    } catch (e) {
      // 403 = not sysadmin: flag forbidden + a specific hint.
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true)
        setError(
          "requires sysadmin — group capabilities are sysadmin-only",
        )
      } else {
        setError(e instanceof Error ? e.message : String(e))
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
      {error && (
        <div className="text-destructive text-sm">{error}</div>
      )}
      {toast && (
        <div className="text-green-700 dark:text-green-400 text-sm">
          {toast}
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
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setName("")
    setIsSysadmin(false)
    setSubmitting(false)
    setError(null)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await routerApi.request(routerGroupsUrl(), {
        method: "POST",
        body: JSON.stringify({ name, is_sysadmin: isSysadmin }),
      })
      await onCreated()
      reset()
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
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
      <DialogContent>
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
            {error && <div className="text-sm text-destructive">{error}</div>}
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
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await routerApi.request(routerGroupUrl(group.group_id), {
        method: "PATCH",
        body: JSON.stringify({ name, is_sysadmin: isSysadmin }),
      })
      await onSaved()
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
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
            {error && <div className="text-sm text-destructive">{error}</div>}
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


function DeleteGroupModal({
  group,
  open,
  onOpenChange,
  onDeleted,
}: {
  group: GroupRow
  open: boolean
  onOpenChange: (open: boolean) => void
  onDeleted: () => void | Promise<void>
}): React.ReactElement {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Type-to-confirm guard (UX-08): the operator must type the group
  // name before the destructive Delete becomes enabled. Prevents a
  // single stray click from wiping a group and its memberships.
  const [confirmText, setConfirmText] = useState("")
  const confirmed = confirmText.trim() === group.name

  // Reset the typed confirmation whenever the modal reopens so a prior
  // (matching) value can't carry over and pre-enable Delete.
  useEffect(() => {
    if (open) {
      setConfirmText("")
      setError(null)
    }
  }, [open])

  const handleDelete = async () => {
    if (!confirmed) return
    setSubmitting(true)
    setError(null)
    try {
      await routerApi.request(routerGroupUrl(group.group_id), {
        method: "DELETE",
      })
      await onDeleted()
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete {group.name}?</DialogTitle>
          <DialogDescription>
            Removes the group and all its memberships. Members
            themselves are not deleted. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2 py-2">
          <Label htmlFor="delete-group-confirm" className="text-sm">
            Type{" "}
            <span className="font-mono font-bold text-destructive">
              {group.name}
            </span>{" "}
            to confirm
          </Label>
          <Input
            id="delete-group-confirm"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder={group.name}
            autoComplete="off"
            disabled={submitting}
          />
        </div>
        {error && (
          <div className="text-sm text-destructive py-2">{error}</div>
        )}
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={handleDelete}
            disabled={submitting || !confirmed}
          >
            {submitting && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
            Delete
          </Button>
        </DialogFooter>
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
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    setError(null)
    void Promise.all([fetchUsers(), fetchGroups()])
      .then(([us, gs]) => {
        setUsers(us)
        // Exclude self from group list to prevent the obvious cycle —
        // deeper cycle detection is Wave 1a's job.
        setGroups(gs.filter((g) => g.group_id !== groupId))
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [open, groupId])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedId) {
      setError("Pick a user or group to add")
      return
    }
    setSubmitting(true)
    setError(null)
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
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSubmitting(false)
    }
  }

  const options = kind === "user" ? users : groups

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Add member to {groupName}</DialogTitle>
            <DialogDescription>
              Add a single user or nest another group.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>Kind</Label>
              <Select
                value={kind}
                onValueChange={(v) => {
                  setKind(v as "user" | "group")
                  setSelectedId("")
                }}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="user">User</SelectItem>
                  <SelectItem value="group">Group</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>{kind === "user" ? "User" : "Group"}</Label>
              {loading ? (
                <div className="text-sm text-muted-foreground">Loading…</div>
              ) : (
                <Select
                  value={selectedId}
                  onValueChange={setSelectedId}
                >
                  <SelectTrigger>
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
            {error && <div className="text-sm text-destructive">{error}</div>}
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
