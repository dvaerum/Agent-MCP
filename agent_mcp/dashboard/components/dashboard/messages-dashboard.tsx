"use client"

import React, { useEffect, useMemo, useState } from "react"
import { MessageSquare, Send, RefreshCw, X, Trash2, MailOpen, Mail } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { apiClient, type Agent } from "@/lib/api"

// Message row shape returned by POST /api/messages/query.
interface Message {
  message_id: string
  sender_id: string
  recipient_id: string
  message_content: string
  message_type: string
  priority: string
  timestamp: string
  delivered: number | boolean
  read: number | boolean
}

interface Filters {
  from: string
  to: string
  type: string
  priority: string
  read: "" | "true" | "false"
  q: string
}

const MESSAGE_TYPES = [
  "text",
  "system",
  "notification",
  "task_update",
  "assistance_request",
]
const PRIORITIES = ["low", "normal", "high", "urgent"]

// Sentinel values for Select dropdowns (Radix Select cannot use ""
// as an item value). "__all" clears the filter, "__broadcast" picks
// the broadcast recipient.
const ALL = "__all"
const BROADCAST = "__broadcast"

// The admin token is fetched once and reused; the dashboard runs as
// admin per ADR-0003. We don't display it.
async function adminToken(): Promise<string> {
  const tokens = await apiClient.getTokens()
  return tokens.admin_token
}

// Helper to call /api/messages* with the JSON-body token convention.
// Listing uses POST /api/messages/query because browsers strip bodies
// from GET requests per the Fetch spec (this was the original bug).
// Compose stays POST /api/messages; mark-read stays PATCH
// /api/messages/<id>; delete is DELETE /api/messages/<id>.
async function callMessages(
  method: "POST" | "PATCH" | "DELETE",
  pathSuffix: string,
  body: Record<string, unknown>
): Promise<any> {
  const base = apiClient.getServerUrl()
  const res = await fetch(`${base}/messages${pathSuffix}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => "")
    throw new Error(txt || `HTTP ${res.status}`)
  }
  return res.json()
}

export function MessagesDashboard() {
  const [messages, setMessages] = useState<Message[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [filters, setFilters] = useState<Filters>({
    from: "",
    to: "",
    type: "",
    priority: "",
    read: "",
    q: "",
  })

  // Per-row selection (message_id set). Cleared after every refresh
  // so we don't accidentally act on stale rows.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  // Compose state.
  const [composeOpen, setComposeOpen] = useState(false)
  const [composeRecipient, setComposeRecipient] = useState("")
  const [composeContent, setComposeContent] = useState("")
  const [composeType, setComposeType] = useState("text")
  const [composePriority, setComposePriority] = useState("normal")
  const [composing, setComposing] = useState(false)

  // Recipient dropdown options: hardcoded admin first (admin always
  // exists but /api/agents may not include it) + workers from
  // /api/agents. Fetched once on mount; reused by the from/to filter
  // dropdowns as well.
  const [agents, setAgents] = useState<Agent[]>([])
  useEffect(() => {
    apiClient.getAgents().then(setAgents).catch(() => setAgents([]))
  }, [])
  const recipientOptions = useMemo(() => {
    const ids = new Set<string>(["admin"])
    for (const a of agents) {
      if (a.agent_id) ids.add(a.agent_id)
    }
    return Array.from(ids)
  }, [agents])

  // Build a body matching the REST contract.
  const queryBody = useMemo(() => {
    return async () => {
      const t = await adminToken()
      const body: Record<string, unknown> = { token: t, limit: 100 }
      if (filters.from) body.from = filters.from
      if (filters.to) body.to = filters.to
      if (filters.type) body.type = filters.type
      if (filters.priority) body.priority = filters.priority
      if (filters.read !== "") body.read = filters.read === "true"
      if (filters.q) body.q = filters.q
      return body
    }
  }, [filters])

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const body = await queryBody()
      const data = await callMessages("POST", "/query", body)
      setMessages(data.messages ?? [])
      // Clear selection — IDs that survive filter changes would
      // silently act on rows the user can no longer see.
      setSelectedIds(new Set())
    } catch (e: any) {
      setError(e.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // refresh on filter changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters])

  const clearFilters = () => {
    setFilters({ from: "", to: "", type: "", priority: "", read: "", q: "" })
  }

  const send = async () => {
    if (!composeRecipient || !composeContent) return
    setComposing(true)
    setError(null)
    try {
      const t = await adminToken()
      // BROADCAST sentinel maps to recipient_id="*" on the backend.
      const recipient =
        composeRecipient === BROADCAST ? "*" : composeRecipient
      await callMessages("POST", "", {
        token: t,
        recipient_id: recipient,
        message_content: composeContent,
        message_type: composeType,
        priority: composePriority,
      })
      setComposeContent("")
      setComposeOpen(false)
      await refresh()
    } catch (e: any) {
      setError(e.message ?? String(e))
    } finally {
      setComposing(false)
    }
  }

  const toggleRead = async (m: Message) => {
    try {
      const t = await adminToken()
      await callMessages("PATCH", `/${m.message_id}`, {
        token: t,
        read: !(m.read === 1 || m.read === true),
      })
      await refresh()
    } catch (e: any) {
      setError(e.message ?? String(e))
    }
  }

  // ----- bulk selection helpers ----------------------------------

  const toggleOne = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const allVisibleSelected =
    messages.length > 0 && selectedIds.size === messages.length

  const toggleAllVisible = () => {
    // selectAllVisible: toggle every row currently rendered (the rows
    // that pass the active filter, since the table renders the full
    // filtered list — no pagination is applied client-side).
    if (allVisibleSelected) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(messages.map((m) => m.message_id)))
    }
  }

  const bulkMark = async (read: boolean) => {
    if (selectedIds.size === 0) return
    setError(null)
    try {
      const t = await adminToken()
      await Promise.all(
        Array.from(selectedIds).map((id) =>
          callMessages("PATCH", `/${id}`, { token: t, read })
        )
      )
      await refresh()
    } catch (e: any) {
      setError(e.message ?? String(e))
    }
  }

  const bulkDelete = async () => {
    if (selectedIds.size === 0) return
    setError(null)
    try {
      const t = await adminToken()
      await Promise.all(
        Array.from(selectedIds).map((id) =>
          callMessages("DELETE", `/${id}`, { token: t })
        )
      )
      await refresh()
    } catch (e: any) {
      setError(e.message ?? String(e))
    }
  }

  const deleteOne = async (m: Message) => {
    setError(null)
    try {
      const t = await adminToken()
      await callMessages("DELETE", `/${m.message_id}`, { token: t })
      await refresh()
    } catch (e: any) {
      setError(e.message ?? String(e))
    }
  }

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <MessageSquare className="h-6 w-6 text-primary" />
          Messages
        </h1>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw className={`h-4 w-4 mr-1 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setComposeOpen((v) => !v)}>
            <Send className="h-4 w-4 mr-1" />
            {composeOpen ? "Close" : "Compose"}
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 text-destructive p-3 text-sm">
          {error}
        </div>
      )}

      {composeOpen && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Compose message</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-3 md:grid-cols-3">
              <div>
                <Label className="text-xs">Recipient agent_id</Label>
                <Select
                  value={composeRecipient}
                  onValueChange={setComposeRecipient}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="select agent" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={BROADCAST}>
                      (broadcast to all workers)
                    </SelectItem>
                    {recipientOptions.map((id) => (
                      <SelectItem key={id} value={id}>{id}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label className="text-xs">Type</Label>
                <Select value={composeType} onValueChange={setComposeType}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {MESSAGE_TYPES.map((t) => (
                      <SelectItem key={t} value={t}>{t}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label className="text-xs">Priority</Label>
                <Select value={composePriority} onValueChange={setComposePriority}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {PRIORITIES.map((p) => (
                      <SelectItem key={p} value={p}>{p}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div>
              <Label className="text-xs">Content</Label>
              <textarea
                className="w-full min-h-[100px] rounded-md border border-input bg-background p-2 text-sm"
                value={composeContent}
                onChange={(e) => setComposeContent(e.target.value)}
                placeholder="Your message"
              />
            </div>
            <div className="flex justify-end">
              <Button onClick={send} disabled={composing || !composeRecipient || !composeContent}>
                {composing ? "Sending…" : "Send"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Filters</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 md:grid-cols-6">
            <Select
              value={filters.from || ALL}
              onValueChange={(v) =>
                setFilters((f) => ({ ...f, from: v === ALL ? "" : v }))
              }
            >
              <SelectTrigger><SelectValue placeholder="from" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>any sender</SelectItem>
                {recipientOptions.map((id) => (
                  <SelectItem key={`from-${id}`} value={id}>{id}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={filters.to || ALL}
              onValueChange={(v) =>
                setFilters((f) => ({ ...f, to: v === ALL ? "" : v }))
              }
            >
              <SelectTrigger><SelectValue placeholder="to" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>any recipient</SelectItem>
                {recipientOptions.map((id) => (
                  <SelectItem key={`to-${id}`} value={id}>{id}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={filters.type || ALL}
              onValueChange={(v) => setFilters((f) => ({ ...f, type: v === ALL ? "" : v }))}
            >
              <SelectTrigger><SelectValue placeholder="type" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>all types</SelectItem>
                {MESSAGE_TYPES.map((t) => (<SelectItem key={t} value={t}>{t}</SelectItem>))}
              </SelectContent>
            </Select>
            <Select
              value={filters.priority || ALL}
              onValueChange={(v) => setFilters((f) => ({ ...f, priority: v === ALL ? "" : v }))}
            >
              <SelectTrigger><SelectValue placeholder="priority" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>any priority</SelectItem>
                {PRIORITIES.map((p) => (<SelectItem key={p} value={p}>{p}</SelectItem>))}
              </SelectContent>
            </Select>
            <Select
              value={filters.read || ALL}
              onValueChange={(v) => setFilters((f) => ({ ...f, read: v === ALL ? "" : (v as "true" | "false") }))}
            >
              <SelectTrigger><SelectValue placeholder="read?" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>any</SelectItem>
                <SelectItem value="false">unread</SelectItem>
                <SelectItem value="true">read</SelectItem>
              </SelectContent>
            </Select>
            <Input
              placeholder="content (substring)"
              value={filters.q}
              onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
            />
          </div>
          <div className="flex justify-end mt-2">
            <Button variant="ghost" size="sm" onClick={clearFilters}>
              <X className="h-4 w-4 mr-1" />
              Clear
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">
            {messages.length} {messages.length === 1 ? "message" : "messages"}
            {selectedIds.size > 0 && (
              <span className="ml-2 text-sm text-muted-foreground">
                ({selectedIds.size} selected)
              </span>
            )}
          </CardTitle>
          {selectedIds.size > 0 && (
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => bulkMark(true)}>
                <MailOpen className="h-4 w-4 mr-1" />
                Mark read
              </Button>
              <Button variant="outline" size="sm" onClick={() => bulkMark(false)}>
                <Mail className="h-4 w-4 mr-1" />
                Mark unread
              </Button>
              <Button variant="destructive" size="sm" onClick={bulkDelete}>
                <Trash2 className="h-4 w-4 mr-1" />
                Delete
              </Button>
            </div>
          )}
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8">
                  <input
                    type="checkbox"
                    aria-label="select all visible"
                    checked={allVisibleSelected}
                    onChange={toggleAllVisible}
                  />
                </TableHead>
                <TableHead>Time</TableHead>
                <TableHead>From</TableHead>
                <TableHead>To</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Pri</TableHead>
                <TableHead>R</TableHead>
                <TableHead>Content</TableHead>
                <TableHead className="w-8"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {messages.map((m) => {
                const checked = selectedIds.has(m.message_id)
                return (
                  <TableRow key={m.message_id}>
                    <TableCell onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        aria-label={`select message ${m.message_id}`}
                        checked={checked}
                        onChange={() => toggleOne(m.message_id)}
                      />
                    </TableCell>
                    <TableCell
                      className="text-xs font-mono cursor-pointer"
                      onClick={() => toggleRead(m)}
                    >
                      {m.timestamp.slice(0, 19)}
                    </TableCell>
                    <TableCell><Badge variant="outline">{m.sender_id}</Badge></TableCell>
                    <TableCell><Badge variant="outline">{m.recipient_id}</Badge></TableCell>
                    <TableCell className="text-xs">{m.message_type}</TableCell>
                    <TableCell className="text-xs">{m.priority}</TableCell>
                    <TableCell
                      className="cursor-pointer"
                      onClick={() => toggleRead(m)}
                    >
                      {m.read === 1 || m.read === true ? "✓" : ""}
                    </TableCell>
                    <TableCell className="max-w-[400px] truncate text-xs">
                      {m.message_content}
                    </TableCell>
                    <TableCell onClick={(e) => e.stopPropagation()}>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label="delete message"
                        onClick={() => deleteOne(m)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
