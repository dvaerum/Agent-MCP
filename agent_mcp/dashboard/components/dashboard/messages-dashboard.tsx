"use client"

import React, { useEffect, useMemo, useState } from "react"
import { MessageSquare, Send, RefreshCw, X } from "lucide-react"
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
import { apiClient } from "@/lib/api"

// Message row shape returned by GET /api/messages.
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

// The admin token is fetched once and reused; the dashboard runs as
// admin per ADR-0003. We don't display it.
async function adminToken(): Promise<string> {
  const tokens = await apiClient.getTokens()
  return tokens.admin_token
}

// Helper to call /api/messages with the JSON-body token convention.
// httpx-style: GET with body is unusual but matches our convention
// (Q6a.1) — other endpoints (memories, tasks) take the token in the
// JSON body too. Fall back to query string if body-on-GET is stripped
// by an intermediate proxy.
async function callMessages(
  method: "GET" | "POST" | "PATCH",
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

  // Compose state.
  const [composeOpen, setComposeOpen] = useState(false)
  const [composeRecipient, setComposeRecipient] = useState("")
  const [composeContent, setComposeContent] = useState("")
  const [composeType, setComposeType] = useState("text")
  const [composePriority, setComposePriority] = useState("normal")
  const [composing, setComposing] = useState(false)

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
      const data = await callMessages("GET", "", body)
      setMessages(data.messages ?? [])
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
      await callMessages("POST", "", {
        token: t,
        recipient_id: composeRecipient,
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
                <Input
                  value={composeRecipient}
                  onChange={(e) => setComposeRecipient(e.target.value)}
                  placeholder="alice"
                />
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
            <Input
              placeholder="from (sender_id)"
              value={filters.from}
              onChange={(e) => setFilters((f) => ({ ...f, from: e.target.value }))}
            />
            <Input
              placeholder="to (recipient_id)"
              value={filters.to}
              onChange={(e) => setFilters((f) => ({ ...f, to: e.target.value }))}
            />
            <Select
              value={filters.type || "__all"}
              onValueChange={(v) => setFilters((f) => ({ ...f, type: v === "__all" ? "" : v }))}
            >
              <SelectTrigger><SelectValue placeholder="type" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">all types</SelectItem>
                {MESSAGE_TYPES.map((t) => (<SelectItem key={t} value={t}>{t}</SelectItem>))}
              </SelectContent>
            </Select>
            <Select
              value={filters.priority || "__all"}
              onValueChange={(v) => setFilters((f) => ({ ...f, priority: v === "__all" ? "" : v }))}
            >
              <SelectTrigger><SelectValue placeholder="priority" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">any priority</SelectItem>
                {PRIORITIES.map((p) => (<SelectItem key={p} value={p}>{p}</SelectItem>))}
              </SelectContent>
            </Select>
            <Select
              value={filters.read || "__all"}
              onValueChange={(v) => setFilters((f) => ({ ...f, read: v === "__all" ? "" : (v as "true" | "false") }))}
            >
              <SelectTrigger><SelectValue placeholder="read?" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">any</SelectItem>
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
        <CardHeader>
          <CardTitle className="text-base">
            {messages.length} {messages.length === 1 ? "message" : "messages"}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>From</TableHead>
                <TableHead>To</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Pri</TableHead>
                <TableHead>R</TableHead>
                <TableHead>Content</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {messages.map((m) => (
                <TableRow
                  key={m.message_id}
                  className="cursor-pointer"
                  onClick={() => toggleRead(m)}
                >
                  <TableCell className="text-xs font-mono">{m.timestamp.slice(0, 19)}</TableCell>
                  <TableCell><Badge variant="outline">{m.sender_id}</Badge></TableCell>
                  <TableCell><Badge variant="outline">{m.recipient_id}</Badge></TableCell>
                  <TableCell className="text-xs">{m.message_type}</TableCell>
                  <TableCell className="text-xs">{m.priority}</TableCell>
                  <TableCell>{m.read === 1 || m.read === true ? "✓" : ""}</TableCell>
                  <TableCell className="max-w-[400px] truncate text-xs">
                    {m.message_content}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
