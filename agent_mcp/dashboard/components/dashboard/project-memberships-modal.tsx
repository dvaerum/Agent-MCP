"use client"

// Project memberships modal — Phase 3 Wave 1b (prancy-napping-pie).
// Opened from a per-project dropdown (in projects-overview-dashboard
// PR follow-up; or directly from a project tab in the sidebar nav).
// Lists who has access (users + groups) with their role
// (operator/viewer); supports add/remove/change-role.
//
// Backend: /agent-mcp/api/router/projects/<name>/memberships[/<id>]

import React, { useCallback, useEffect, useState } from "react"
import {
  Loader2, Plus, Trash2, ShieldAlert, User as UserIcon, Users as UsersIcon,
} from "lucide-react"
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
import { Label } from "@/components/ui/label"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { EmptyState } from "@/components/dashboard/shared/empty-state"
import {
  projectMembershipsUrl, projectMembershipUrl,
  routerUsersUrl, routerGroupsUrl,
} from "@/lib/urls"
import { routerApi } from "@/lib/router-api"
import { ApiError } from "@/lib/api"
import {
  useProjectMembershipsQuery,
  type Role,
  type MembershipRow,
} from "@/lib/queries/project-memberships"
import { invalidateProjectMemberships } from "@/lib/query-client"
import { toastUndo } from "@/components/ui/toast"

interface UserOption {
  user_id: string
  username: string
}

interface GroupOption {
  group_id: string
  name: string
}


export interface ProjectMembershipsModalProps {
  projectName: string
  open: boolean
  onOpenChange: (open: boolean) => void
}


export function ProjectMembershipsModal({
  projectName,
  open,
  onOpenChange,
}: ProjectMembershipsModalProps): React.ReactElement {
  const query = useProjectMembershipsQuery(projectName, open)
  const rows = query.data ?? []
  // useRouterQuery folded a 403 into a dedicated `forbidden` flag; useQuery
  // surfaces it as a thrown ApiError on `error`. Re-derive the split (as
  // groups-dashboard.tsx does) so the "Sysadmin only" card still outranks
  // a raw fetch-error string. Mutation errors stay in their own state and
  // render through the same error slot as before.
  const forbidden =
    query.error instanceof ApiError && query.error.status === 403
  const fetchErrorMsg = forbidden ? null : (query.error?.message ?? null)
  const loading = query.isFetching
  const [mutationError, setMutationError] = useState<string | null>(null)
  const error = mutationError ?? fetchErrorMsg
  const [addOpen, setAddOpen] = useState(false)
  // Freshness after a membership mutation rides an explicit
  // invalidateProjectMemberships() — the direct replacement for
  // useRouterQuery's `refresh()` (no SSE at the overview).
  const refresh = useCallback(
    () => invalidateProjectMemberships(projectName),
    [projectName],
  )

  // Mirrors the old shared-``error``-state's synchronous reset at the
  // start of every ``refresh()`` — a stale mutation error (e.g. a
  // failed remove) must not linger once a fresh GET (reopen, or a
  // ``projectName`` change) starts.
  useEffect(() => {
    if (loading) setMutationError(null)
  }, [loading])

  // One `project_membership` row, re-grantable by the POST the
  // Add-membership modal already makes — so no confirm dialog, but the
  // reversal is offered instead of the silent success this used to be.
  // The row's ROLE is carried through: an Undo that re-added the member
  // as `operator` when they had been `viewer` would be a privilege
  // escalation dressed up as a restore.
  const handleRemove = async (row: MembershipRow) => {
    const label = row.username || row.group_name || row.membership_id
    const body = row.user_id
      ? { user_id: row.user_id, role: row.role }
      : { group_id: row.group_id, role: row.role }
    try {
      await routerApi.request(
        projectMembershipUrl(projectName, row.membership_id),
        { method: "DELETE" },
      )
      setMutationError(null)
      refresh()
      toastUndo(
        `Removed ${label} from ${projectName}.`,
        async () => {
          await routerApi.request(projectMembershipsUrl(projectName), {
            method: "POST",
            body: JSON.stringify(body),
          })
          refresh()
        },
        {
          undoneMessage: `${label} restored to ${projectName} as ${row.role}.`,
        },
      )
    } catch (e) {
      setMutationError(e instanceof Error ? e.message : String(e))
    }
  }

  const handleChangeRole = async (id: string, role: Role) => {
    try {
      await routerApi.request(projectMembershipUrl(projectName, id), {
        method: "PATCH",
        body: JSON.stringify({ role }),
      })
      setMutationError(null)
      refresh()
    } catch (e) {
      setMutationError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* CC-14: `w-[calc(100vw-2rem)]` keeps a 1rem gutter each side at
          375px; without it the sm:max-w-3xl width applies below sm too
          and the dialog clips horizontally. */}
      {/* max-h + flex-col caps the dialog to the viewport (dvh, not vh,
          for mobile Safari/Chrome's collapsing address bar); the
          membership table can grow arbitrarily long, so it needs its
          own scroll region rather than pushing Close off-screen — see
          docs/learnings/dashboard-dialog-mobile-clipping.md. */}
      <DialogContent className="w-[calc(100vw-2rem)] flex max-h-[calc(100dvh-2rem)] flex-col overflow-hidden sm:!max-w-3xl">
        <DialogHeader className="flex-shrink-0">
          <DialogTitle>Memberships for {projectName}</DialogTitle>
          <DialogDescription>
            Users and groups with access to this project. Roles:
            operator (full) or viewer (read-only).
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 min-h-0 space-y-4 overflow-y-auto py-2 pr-1">
          <div className="flex justify-end">
            <Button size="sm" onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4 mr-1" /> Add
            </Button>
          </div>
          {loading && (
            <div className="flex items-center gap-2 text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
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
                You don&apos;t have sysadmin privileges. Ask a sysadmin to
                view or manage this project&apos;s memberships on your
                behalf.
              </CardContent>
            </Card>
          )}
          {!loading && !forbidden && error && (
            <div className="text-destructive text-sm">{error}</div>
          )}
          {/* CC-20-style swap: this used to be a bare centered TableCell
              string ("No memberships.") — the one hand-rolled empty-state
              left in the dashboard once messages-dashboard adopted the
              shared primitive. The table itself stays hand-rolled
              (ResponsiveDataTable inside a dialog is an unproven pattern
              with no existing precedent — out of scope here). */}
          {!loading && !forbidden && rows.length === 0 && (
            <EmptyState
              icon={UsersIcon}
              title="No memberships"
              description="No users or groups have access to this project yet."
            />
          )}
          {!loading && !forbidden && rows.length > 0 && (
            // CC-14-capped dialog width means an unwrapped table clips
            // silently rather than reflowing — wrap it so an overflow
            // scrolls horizontally instead (pinned by
            // test_dialog_hosted_tables_scroll_horizontally_not_clip).
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Kind</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead>Role</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((r) => {
                    const isUser = !!r.user_id
                    return (
                      <TableRow key={r.membership_id}>
                        <TableCell>
                          {isUser ? (
                            <UserIcon className="h-4 w-4 text-muted-foreground" />
                          ) : (
                            <UsersIcon className="h-4 w-4 text-muted-foreground" />
                          )}
                        </TableCell>
                        <TableCell className="font-medium">
                          {r.username || r.group_name}
                        </TableCell>
                        <TableCell>
                          <Select
                            value={r.role}
                            onValueChange={(v) =>
                              handleChangeRole(r.membership_id, v as Role)
                            }
                          >
                            <SelectTrigger className="h-7 w-32">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="operator">operator</SelectItem>
                              <SelectItem value="viewer">viewer</SelectItem>
                            </SelectContent>
                          </Select>
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`Remove ${r.username || r.group_name}`}
                            className="text-destructive"
                            onClick={() => handleRemove(r)}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
        <DialogFooter className="flex-shrink-0">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
        <AddMembershipModal
          projectName={projectName}
          open={addOpen}
          onOpenChange={setAddOpen}
          onAdded={refresh}
        />
      </DialogContent>
    </Dialog>
  )
}


function AddMembershipModal({
  projectName,
  open,
  onOpenChange,
  onAdded,
}: {
  projectName: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onAdded: () => void | Promise<void>
}): React.ReactElement {
  const [kind, setKind] = useState<"user" | "group">("user")
  const [users, setUsers] = useState<UserOption[]>([])
  const [groups, setGroups] = useState<GroupOption[]>([])
  const [selectedId, setSelectedId] = useState("")
  const [role, setRole] = useState<Role>("operator")
  const [loading, setLoading] = useState(false)
  // Separate from submit errors (which <FormDialog> surfaces as a
  // toast): this fires in a useEffect outside onSubmit's flow, so it
  // needs its own inline surface.
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    setLoadError(null)
    void Promise.all([
      routerApi.request<{ users?: UserOption[] }>(routerUsersUrl()),
      routerApi.request<{ groups?: GroupOption[] }>(routerGroupsUrl()),
    ])
      .then(([u, g]) => {
        setUsers(u.users || [])
        setGroups(g.groups || [])
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [open])

  const handleSubmit = async () => {
    const body =
      kind === "user"
        ? { user_id: selectedId, role }
        : { group_id: selectedId, role }
    await routerApi.request(projectMembershipsUrl(projectName), {
      method: "POST",
      body: JSON.stringify(body),
    })
    await onAdded()
    setSelectedId("")
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Add membership to ${projectName}`}
      onSubmit={handleSubmit}
      submitLabel="Add"
      submitDisabled={!selectedId}
      successMessage="Membership added."
      errorMessage="Failed to add membership"
    >
      <div className="space-y-2">
        <Label htmlFor="pm-kind">Kind</Label>
        <Select
          value={kind}
          onValueChange={(v) => {
            setKind(v as "user" | "group")
            setSelectedId("")
          }}
        >
          <SelectTrigger id="pm-kind">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="user">User</SelectItem>
            <SelectItem value="group">Group</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-2">
        <Label htmlFor="pm-entity">{kind === "user" ? "User" : "Group"}</Label>
        {loading ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : (
          <Select value={selectedId} onValueChange={setSelectedId}>
            <SelectTrigger id="pm-entity">
              <SelectValue placeholder={`Select a ${kind}`} />
            </SelectTrigger>
            <SelectContent>
              {(kind === "user" ? users : groups).length === 0 && (
                <SelectItem disabled value="__none__">
                  None available
                </SelectItem>
              )}
              {kind === "user"
                ? users.map((u) => (
                    <SelectItem key={u.user_id} value={u.user_id}>
                      {u.username}
                    </SelectItem>
                  ))
                : groups.map((g) => (
                    <SelectItem key={g.group_id} value={g.group_id}>
                      {g.name}
                    </SelectItem>
                  ))}
            </SelectContent>
          </Select>
        )}
      </div>
      <div className="space-y-2">
        <Label htmlFor="pm-role">Role</Label>
        <Select value={role} onValueChange={(v) => setRole(v as Role)}>
          <SelectTrigger id="pm-role">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="operator">operator</SelectItem>
            <SelectItem value="viewer">viewer</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {loadError && (
        <div className="text-sm text-destructive">
          Failed to load users/groups: {loadError}
        </div>
      )}
    </FormDialog>
  )
}
