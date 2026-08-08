"use client"

// Router-level user list view — Phase 3 Wave 1b (prancy-napping-pie).
// Lives at the cross-project overview (``/agent-mcp/app/``) as a
// tabbed section alongside the existing Projects tab; rendered by
// ProjectsOverviewDashboard via the same Tabs component used for
// the project listing. Per-project pages don't show this — user
// management is a router-level concern.
//
// Backend: /agent-mcp/api/router/users[/<user_id>] (Wave 1b REST).
// Cookie session carries auth; no body-token field.
//
// Presentation is delegated to the shared <DataTablePage> scaffold
// (see components/dashboard/shared/data-table-page.tsx and the
// memories-dashboard reference migration): header, loading skeleton,
// the "Sysadmin only" 403 panel, the list-load error panel, the empty
// state and the desktop/mobile table all live there now. This file
// owns only the data source (useRouterQuery), the column spec and the
// create/edit/delete modals.

import React, { useCallback, useMemo, useState } from "react"
import { Loader2, Plus, Pencil, Trash2, Shield, Users } from "lucide-react"
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
import { routerUsersUrl, routerUserUrl } from "@/lib/urls"
import { routerApi } from "@/lib/router-api"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { useRouterQuery } from "@/hooks/use-router-query"
import { DataTablePage } from "@/components/dashboard/shared/data-table-page"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"
import { DeleteConfirmModal } from "./modals/delete-confirm-modal"

// Client-side hint that MUST be kept in sync with the server's canonical
// policy: `identity.PASSWORD_MIN_LENGTH` / `validate_password_strength`
// (agent_mcp/router/identity.py). The TS build can't import the Python
// constant, so this value is duplicated here purely so the client rejects
// too-short passwords with a clear inline hint instead of letting the
// server return an opaque 400. Update both together if the policy changes.
const PASSWORD_MIN_LENGTH = 12

interface UserRow {
  user_id: string
  username: string
  email: string | null
  is_sysadmin: boolean
  created_at: string
  last_login_at: string | null
}

interface ListResponse {
  success: boolean
  users: UserRow[]
}

async function fetchUsers(signal: AbortSignal): Promise<UserRow[]> {
  const body = await routerApi.request<ListResponse>(routerUsersUrl(), { signal })
  return body.users || []
}

export function UsersDashboard(): React.ReactElement {
  const {
    data,
    loading,
    error: fetchError,
    forbidden,
    refresh,
  } = useRouterQuery<UserRow[]>(fetchUsers)
  const users = data ?? []
  const error = fetchError?.message ?? null
  const [addOpen, setAddOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<UserRow | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<UserRow | null>(null)

  // Delete is owned here (was the inline DeleteUserModal): the shared
  // <DeleteConfirmModal> renders the type-to-confirm gate and keeps
  // itself open on failure when we re-throw.
  const deleteUser = useCallback(
    async (user: UserRow) => {
      try {
        await routerApi.request(routerUserUrl(user.user_id), {
          method: "DELETE",
        })
        refresh()
        toastSuccess(`User "${user.username}" deleted.`)
      } catch (e) {
        toastError(e, "Failed to delete user")
        throw e
      }
    },
    [refresh],
  )

  const columns: Column<UserRow>[] = useMemo(
    () => [
      {
        id: "username",
        header: "Username",
        mobileLabel: "Username",
        cellClassName: "font-medium",
        cell: (u) => u.username,
      },
      {
        id: "email",
        header: "Email",
        mobileLabel: "Email",
        cellClassName: "text-muted-foreground",
        cell: (u) => u.email ?? "—",
      },
      {
        id: "sysadmin",
        header: "Sysadmin",
        mobileLabel: "Sysadmin",
        cell: (u) =>
          u.is_sysadmin ? (
            <Badge variant="default" className="flex items-center gap-1 w-fit">
              <Shield className="h-3 w-3" /> sysadmin
            </Badge>
          ) : (
            <span className="text-muted-foreground">—</span>
          ),
      },
      {
        id: "last_login",
        header: "Last login",
        mobileLabel: "Last login",
        cellClassName: "text-muted-foreground text-xs",
        cell: (u) => (u.last_login_at ? u.last_login_at.slice(0, 19) : "never"),
      },
      {
        id: "actions",
        header: "Actions",
        headClassName: "text-right",
        cellClassName: "text-right",
        cell: (u) => (
          <>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Edit ${u.username}`}
              onClick={() => setEditTarget(u)}
            >
              <Pencil className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Delete ${u.username}`}
              className="text-destructive"
              onClick={() => setDeleteTarget(u)}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </>
        ),
      },
    ],
    [],
  )

  return (
    <DataTablePage<UserRow>
      header={{
        title: "Users",
        subtitle: "Operator accounts and sysadmin status",
        onRefresh: refresh,
        refreshing: loading,
        actions: (
          <Button onClick={() => setAddOpen(true)} size="sm">
            <Plus className="h-4 w-4 mr-1" /> Add user
          </Button>
        ),
      }}
      loading={loading}
      error={error}
      forbidden={forbidden}
      columns={columns}
      rows={users}
      getRowId={(u) => u.user_id}
      empty={{
        icon: Users,
        title: "No users yet",
        description: "Add an operator account to get started.",
      }}
    >
      <AddUserModal open={addOpen} onOpenChange={setAddOpen} onCreated={refresh} />
      {editTarget && (
        <EditUserModal
          user={editTarget}
          open={true}
          onOpenChange={(o) => !o && setEditTarget(null)}
          onSaved={refresh}
        />
      )}
      {/* Type-the-username-to-confirm delete (shared modal, matchCase —
          the confirmation word is a case-sensitive account name, not the
          generic "DELETE"). Mounted only while a target is selected so
          the modal's confirm-input state resets between selections. */}
      {deleteTarget && (
        <DeleteConfirmModal
          open={true}
          onOpenChange={(o) => !o && setDeleteTarget(null)}
          entityLabel="User"
          requiredWord={deleteTarget.username}
          matchCase
          inputId="delete-user-confirm"
          title={`Delete ${deleteTarget.username}?`}
          description="This permanently removes the account, all its sessions, and all its project memberships. This action cannot be undone."
          warningText="The account, all its sessions and all its project memberships will be permanently removed. This action cannot be reversed."
          confirmLabel="Delete user"
          onConfirm={() => deleteUser(deleteTarget)}
        />
      )}
    </DataTablePage>
  )
}

// ── Modals ────────────────────────────────────────────────────────


function AddUserModal({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: () => void | Promise<void>
}): React.ReactElement {
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [email, setEmail] = useState("")
  const [isSysadmin, setIsSysadmin] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const passwordTooShort =
    password.length > 0 && password.length < PASSWORD_MIN_LENGTH

  const reset = () => {
    setUsername("")
    setPassword("")
    setEmail("")
    setIsSysadmin(false)
    setSubmitting(false)
  }

  // Mutation errors go through the shared toast (architecture review
  // Class 1) — no per-modal error state / inline banner.
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      await routerApi.request(routerUsersUrl(), {
        method: "POST",
        body: JSON.stringify({
          username,
          password,
          email: email || undefined,
          is_sysadmin: isSysadmin,
        }),
      })
      await onCreated()
      toastSuccess(`User "${username}" created.`)
      reset()
      onOpenChange(false)
    } catch (e) {
      toastError(e, "Failed to create user")
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
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-lg">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Add user</DialogTitle>
            <DialogDescription>
              Create a new operator account. Password must be at least{" "}
              {PASSWORD_MIN_LENGTH} characters.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="add-user-username">Username</Label>
              <Input
                id="add-user-username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="add-user-password">Password</Label>
              <Input
                id="add-user-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={PASSWORD_MIN_LENGTH}
                aria-invalid={passwordTooShort}
                required
              />
              {passwordTooShort && (
                <p className="text-xs text-destructive">
                  Password must be at least {PASSWORD_MIN_LENGTH} characters.
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="add-user-email">Email (optional)</Label>
              <Input
                id="add-user-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={isSysadmin}
                onChange={(e) => setIsSysadmin(e.target.checked)}
              />
              Grant sysadmin
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
            <Button
              type="submit"
              disabled={submitting || password.length < PASSWORD_MIN_LENGTH}
            >
              {submitting && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}


function EditUserModal({
  user,
  open,
  onOpenChange,
  onSaved,
}: {
  user: UserRow
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved: () => void | Promise<void>
}): React.ReactElement {
  const [isSysadmin, setIsSysadmin] = useState(user.is_sysadmin)
  const [email, setEmail] = useState(user.email ?? "")
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      await routerApi.request(routerUserUrl(user.user_id), {
        method: "PATCH",
        body: JSON.stringify({
          is_sysadmin: isSysadmin,
          email: email || null,
        }),
      })
      await onSaved()
      toastSuccess(`User "${user.username}" updated.`)
      onOpenChange(false)
    } catch (e) {
      toastError(e, "Failed to update user")
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-lg">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Edit {user.username}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="edit-user-email">Email</Label>
              <Input
                id="edit-user-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={isSysadmin}
                onChange={(e) => setIsSysadmin(e.target.checked)}
              />
              Grant sysadmin
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
