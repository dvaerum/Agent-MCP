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

import React, { useCallback, useEffect, useState } from "react"
import { Loader2, Plus, Pencil, Trash2, Shield } from "lucide-react"
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { routerUsersUrl, routerUserUrl } from "@/lib/urls"

const STRICT_HEADERS = {
  Accept: "application/vnd.agent-mcp.v1+json",
  "Content-Type": "application/json",
}

// Mirrors the router's `_PASSWORD_MIN_LENGTH` (admin_users_api.py). Kept
// in sync so the client rejects too-short passwords with a clear inline
// hint instead of letting the server return an opaque 400.
const PASSWORD_MIN_LENGTH = 8

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

interface ErrorResponse {
  success: false
  error: string
  message: string
}

async function fetchUsers(): Promise<UserRow[]> {
  const r = await fetch(routerUsersUrl(), {
    headers: { Accept: STRICT_HEADERS.Accept },
    credentials: "include",
  })
  if (!r.ok) {
    throw new Error(`HTTP ${r.status}`)
  }
  const body: ListResponse = await r.json()
  return body.users || []
}

export function UsersDashboard(): React.ReactElement {
  const [users, setUsers] = useState<UserRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<UserRow | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<UserRow | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setUsers(await fetchUsers())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <div className="flex flex-col h-full w-full">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between px-[var(--space-fluid-lg)] py-[var(--space-fluid-md)] gap-[var(--space-fluid-sm)] border-b bg-background/95">
        <div>
          <h1 className="text-fluid-2xl font-bold tracking-tight">Users</h1>
          <p className="text-fluid-base text-muted-foreground mt-1">
            Operator accounts and sysadmin status
          </p>
        </div>
        <Button onClick={() => setAddOpen(true)} size="sm">
          <Plus className="h-4 w-4 mr-1" /> Add user
        </Button>
      </div>

      <div className="flex-1 overflow-auto p-[var(--space-fluid-lg)]">
        {loading && (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading users…
          </div>
        )}
        {error && (
          <div className="text-destructive text-sm">Error: {error}</div>
        )}
        {!loading && !error && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Username</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Sysadmin</TableHead>
                <TableHead>Last login</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground">
                    No users yet.
                  </TableCell>
                </TableRow>
              )}
              {users.map((u) => (
                <TableRow key={u.user_id}>
                  <TableCell className="font-medium">{u.username}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {u.email ?? "—"}
                  </TableCell>
                  <TableCell>
                    {u.is_sysadmin ? (
                      <Badge variant="default" className="flex items-center gap-1 w-fit">
                        <Shield className="h-3 w-3" /> sysadmin
                      </Badge>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {u.last_login_at ? u.last_login_at.slice(0, 19) : "never"}
                  </TableCell>
                  <TableCell className="text-right">
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
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <AddUserModal
        open={addOpen}
        onOpenChange={setAddOpen}
        onCreated={refresh}
      />
      {editTarget && (
        <EditUserModal
          user={editTarget}
          open={true}
          onOpenChange={(o) => !o && setEditTarget(null)}
          onSaved={refresh}
        />
      )}
      {deleteTarget && (
        <DeleteUserModal
          user={deleteTarget}
          open={true}
          onOpenChange={(o) => !o && setDeleteTarget(null)}
          onDeleted={refresh}
        />
      )}
    </div>
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
  const [error, setError] = useState<string | null>(null)

  const passwordTooShort =
    password.length > 0 && password.length < PASSWORD_MIN_LENGTH

  const reset = () => {
    setUsername("")
    setPassword("")
    setEmail("")
    setIsSysadmin(false)
    setSubmitting(false)
    setError(null)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const r = await fetch(routerUsersUrl(), {
        method: "POST",
        headers: STRICT_HEADERS,
        credentials: "include",
        body: JSON.stringify({
          username,
          password,
          email: email || undefined,
          is_sysadmin: isSysadmin,
        }),
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
            <DialogTitle>Add user</DialogTitle>
            <DialogDescription>
              Create a new operator account. Password must be at least 8
              characters.
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
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const r = await fetch(routerUserUrl(user.user_id), {
        method: "PATCH",
        headers: STRICT_HEADERS,
        credentials: "include",
        body: JSON.stringify({
          is_sysadmin: isSysadmin,
          email: email || null,
        }),
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


function DeleteUserModal({
  user,
  open,
  onOpenChange,
  onDeleted,
}: {
  user: UserRow
  open: boolean
  onOpenChange: (open: boolean) => void
  onDeleted: () => void | Promise<void>
}): React.ReactElement {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Type-to-confirm guard: the operator must type the exact username
  // before the destructive delete unlocks. The parent unmounts this
  // modal when `deleteTarget` clears between selections, so this state
  // resets naturally on the next open.
  const [confirmText, setConfirmText] = useState("")
  const confirmed = confirmText === user.username

  const handleDelete = async () => {
    if (!confirmed) return
    setSubmitting(true)
    setError(null)
    try {
      const r = await fetch(routerUserUrl(user.user_id), {
        method: "DELETE",
        headers: { Accept: STRICT_HEADERS.Accept },
        credentials: "include",
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
          <DialogTitle>Delete {user.username}?</DialogTitle>
          <DialogDescription>
            This permanently removes the account, all its sessions, and
            all its project memberships. This action cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2 py-2">
          <Label htmlFor="delete-user-confirm">
            Type <span className="font-mono font-semibold">{user.username}</span>{" "}
            to confirm
          </Label>
          <Input
            id="delete-user-confirm"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder={user.username}
            autoComplete="off"
            autoFocus
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
