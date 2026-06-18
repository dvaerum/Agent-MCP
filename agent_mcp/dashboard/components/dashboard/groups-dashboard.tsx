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
import {
  routerGroupsUrl, routerGroupUrl,
  routerGroupMembersUrl, routerGroupMemberUrl,
  routerUsersUrl,
} from "@/lib/urls"

const STRICT_HEADERS = {
  Accept: "application/vnd.agent-mcp.v1+json",
  "Content-Type": "application/json",
}

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

interface ErrorResponse {
  success: false
  error: string
  message: string
}

async function fetchGroups(): Promise<GroupRow[]> {
  const r = await fetch(routerGroupsUrl(), {
    headers: { Accept: STRICT_HEADERS.Accept },
    credentials: "include",
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()).groups || []
}

async function fetchMembers(groupId: string): Promise<MemberRow[]> {
  const r = await fetch(routerGroupMembersUrl(groupId), {
    headers: { Accept: STRICT_HEADERS.Accept },
    credentials: "include",
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()).members || []
}

async function fetchUsers(): Promise<UserRow[]> {
  const r = await fetch(routerUsersUrl(), {
    headers: { Accept: STRICT_HEADERS.Accept },
    credentials: "include",
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()).users || []
}


export function GroupsDashboard(): React.ReactElement {
  const [groups, setGroups] = useState<GroupRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<GroupRow | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<GroupRow | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setGroups(await fetchGroups())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

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
        {error && (
          <div className="text-destructive text-sm">Error: {error}</div>
        )}
        {!loading && !error && groups.length === 0 && (
          <div className="text-muted-foreground text-center py-8">
            No groups yet.
          </div>
        )}
        {!loading && !error && groups.map((g) => (
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
      const r = await fetch(
        routerGroupMemberUrl(group.group_id, memberId),
        {
          method: "DELETE",
          headers: { Accept: STRICT_HEADERS.Accept },
          credentials: "include",
        },
      )
      if (!r.ok) {
        const body = (await r.json().catch(() => ({}))) as ErrorResponse
        throw new Error(body.message || `HTTP ${r.status}`)
      }
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
      const r = await fetch(routerGroupsUrl(), {
        method: "POST",
        headers: STRICT_HEADERS,
        credentials: "include",
        body: JSON.stringify({ name, is_sysadmin: isSysadmin }),
      })
      const body = (await r.json().catch(() => ({}))) as
        | ErrorResponse
        | { success: true }
      if (!r.ok || (body as ErrorResponse).success === false) {
        throw new Error(
          (body as ErrorResponse).message ||
            (body as ErrorResponse).error ||
            `HTTP ${r.status}`,
        )
      }
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
      const r = await fetch(routerGroupUrl(group.group_id), {
        method: "PATCH",
        headers: STRICT_HEADERS,
        credentials: "include",
        body: JSON.stringify({ name, is_sysadmin: isSysadmin }),
      })
      const body = (await r.json().catch(() => ({}))) as
        | ErrorResponse
        | { success: true }
      if (!r.ok || (body as ErrorResponse).success === false) {
        throw new Error(
          (body as ErrorResponse).message ||
            (body as ErrorResponse).error ||
            `HTTP ${r.status}`,
        )
      }
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

  const handleDelete = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const r = await fetch(routerGroupUrl(group.group_id), {
        method: "DELETE",
        headers: { Accept: STRICT_HEADERS.Accept },
        credentials: "include",
      })
      if (!r.ok) {
        const body = (await r.json().catch(() => ({}))) as ErrorResponse
        throw new Error(body.message || `HTTP ${r.status}`)
      }
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
            themselves are not deleted.
          </DialogDescription>
        </DialogHeader>
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
            disabled={submitting}
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
      const r = await fetch(routerGroupMembersUrl(groupId), {
        method: "POST",
        headers: STRICT_HEADERS,
        credentials: "include",
        body: JSON.stringify(body),
      })
      const respBody = (await r.json().catch(() => ({}))) as
        | ErrorResponse
        | { success: true }
      if (!r.ok || (respBody as ErrorResponse).success === false) {
        throw new Error(
          (respBody as ErrorResponse).message ||
            (respBody as ErrorResponse).error ||
            `HTTP ${r.status}`,
        )
      }
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
