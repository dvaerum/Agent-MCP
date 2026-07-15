"use client"

import React, { useCallback, useEffect, useMemo, useState } from "react"
import {
  MessageSquare,
  Send,
  RefreshCw,
  X,
  Trash2,
  MailOpen,
  Mail,
  Plus,
  Search,
  CheckSquare,
} from "lucide-react"
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
import { apiClient, type Message } from "@/lib/api"
import { cn } from "@/lib/utils"
import { useDialog } from "@/hooks/use-dialog"
import { useFilters } from "@/hooks/use-filters"
import { usePagedQuery } from "@/hooks/use-paged-query"
import { useServerStore } from "@/lib/stores/server-store"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/dashboard/shared/empty-state"
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { MessagesMobileList } from "@/components/dashboard/messages-mobile-list"
import { ViewMessageModal } from "@/components/dashboard/modals/view-message-modal"
import { DeleteMessageModal } from "@/components/dashboard/modals/delete-message-modal"
import { toastError, toastSuccess } from "@/components/ui/toast"

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

// v5.0.26: pagination footer on the messages list. Per-page size stays
// at 100 (Dennis explicitly does not want bigger pages — see plan
// prancy-napping-pie.md). The footer adds « Newest / Newer / Older /
// Oldest » cursor buttons + a "Showing N–M of T" range label so admins
// can reach messages past the first 100 rows. Component state only —
// no URL state, matches the existing filter behavior.
const PAGE_SIZE = 100

// Wave 2 (cleanup-wave-2): the ``adminToken()`` helper is gone.
// Dashboard mutations authenticate via the operator session cookie
// set on /agent-mcp/login — the browser attaches it to every fetch
// automatically (the apiClient helper and the local ``callMessages``
// helper both opt into ``credentials: 'include'``).

// Helper to call /api/messages* under cookie auth.
// Listing uses POST /api/messages/query because browsers strip bodies
// from GET requests per the Fetch spec (this was the original bug).
// Compose stays POST /api/messages; mark-read stays PATCH
// /api/messages/<id>; delete is DELETE /api/messages/<id>.
//
// Wave 2 (cleanup-wave-2): ``credentials: "include"`` ensures the
// ``agent_mcp_session`` cookie travels with the request even on the
// dashboard's cross-origin-but-same-site dev URLs; the request body
// no longer carries a bearer token, so missing the cookie would
// surface as the backend's 401 login_required envelope.
async function callMessages(
  method: "POST" | "PATCH" | "DELETE",
  pathSuffix: string,
  body: Record<string, unknown>
): Promise<any> {
  const base = apiClient.getServerUrl()
  const res = await fetch(`${base}/messages${pathSuffix}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      // PR-A: REST endpoints require the strict v1 media type.
      "Accept": "application/vnd.agent-mcp.v1+json",
    },
    body: JSON.stringify(body),
    credentials: "include",
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => "")
    throw new Error(txt || `HTTP ${res.status}`)
  }
  return res.json()
}

// Stats card component — matches the Agents/Tasks/Memories StatsCard
// (plain Tailwind sizing + rounded-lg + semantic tokens + tabular-nums).
const StatsCard = ({ icon: Icon, label, value, change, trend }: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: number
  change?: string
  trend?: 'up' | 'down' | 'neutral'
}) => (
  <div className="bg-card border border-border rounded-lg p-3 sm:p-5 hover:bg-muted/30 transition-colors duration-150 group">
    <div className="flex items-center justify-between">
      <div>
        <div className="flex items-center gap-2 mb-2">
          <Icon className="h-4 w-4 text-muted-foreground transition-colors" />
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">{label}</span>
        </div>
        <div className="text-2xl sm:text-3xl font-semibold text-foreground tabular-nums mb-1">{value}</div>
        {change && (
          <div className={cn(
            "text-xs font-medium tabular-nums",
            trend === 'up' && "text-emerald-500",
            trend === 'down' && "text-destructive",
            trend === 'neutral' && "text-muted-foreground"
          )}>
            {change}
          </div>
        )}
      </div>
    </div>
  </div>
)

// Message table row — extracted for parity with memories' <MemoryRow>.
// Row-body click opens the detail modal; the checkbox + delete cells
// stopPropagation so they don't also fire the row-open.
const MessageRow = ({
  message: m,
  selected,
  onToggle,
  onOpenDetail,
  onDelete,
  labelForParent,
}: {
  message: Message
  selected: boolean
  onToggle: (id: string) => void
  onOpenDetail: (m: Message) => void
  onDelete: (m: Message) => void
  labelForParent: (parentId: string | null) => string
}) => {
  // v5.0.22: rows whose parent_message_id is non-null are replies.
  // Visual cue = subtle left border + "↳ reply to: <parent>" prefix in
  // the Subject column.
  const isReply = !!m.parent_message_id
  const isRead = m.read === 1 || m.read === true
  return (
    <TableRow
      className={
        "cursor-pointer" +
        (isReply ? " border-l-2 border-l-muted-foreground/30" : "")
      }
      onClick={() => onOpenDetail(m)}
    >
      <TableCell onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          aria-label={`select message ${m.message_id}`}
          checked={selected}
          onChange={() => onToggle(m.message_id)}
        />
      </TableCell>
      <TableCell className="text-xs font-mono tabular-nums">
        {/* Per-row entity glyph (matches memories' <Brain> convention). */}
        <div className="flex items-center gap-2">
          <MessageSquare className="h-3 w-3 text-primary flex-shrink-0" />
          <span>{m.timestamp.slice(0, 19)}</span>
        </div>
      </TableCell>
      <TableCell><Badge variant="outline">{m.sender_id}</Badge></TableCell>
      <TableCell><Badge variant="outline">{m.recipient_id}</Badge></TableCell>
      <TableCell className="text-xs max-w-[200px] truncate">
        {m.subject ? (
          m.subject
        ) : isReply ? (
          // v5.0.24 polish: human-readable parent label instead of the
          // opaque message_id.
          <span className="text-muted-foreground">
            ↳ reply to:{" "}
            <span className="text-foreground">
              {labelForParent(m.parent_message_id)}
            </span>
          </span>
        ) : (
          <span className="text-muted-foreground/50">—</span>
        )}
      </TableCell>
      <TableCell className="text-xs">{m.message_type}</TableCell>
      <TableCell className="text-xs">{m.priority}</TableCell>
      <TableCell>{isRead ? "✓" : ""}</TableCell>
      <TableCell className="max-w-[400px] truncate text-xs">
        {m.message_content}
      </TableCell>
      <TableCell onClick={(e) => e.stopPropagation()}>
        <Button
          variant="ghost"
          size="sm"
          aria-label="delete message"
          className="text-destructive hover:text-destructive hover:bg-destructive/10"
          onClick={() => onDelete(m)}
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      </TableCell>
    </TableRow>
  )
}

export function MessagesDashboard() {
  // Server-online indicator (matches Agents/Tasks/Memories header).
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find((s) => s.id === activeServerId)

  // v5.0.26: pagination cursor. Total comes from the hook. Declared
  // before `filters` because the useFilters() `onReset` callback below
  // closes over `setCurrentOffset`.
  const [currentOffset, setCurrentOffset] = useState(0)

  // Filter state — owned by useFilters<Filters>. `onReset` preserves
  // the v5.0.26 "filter changed -> page 1" semantics.
  const {
    filters,
    setFilter,
    clearAll: clearFilters,
  } = useFilters<Filters>({
    initial: {
      from: "",
      to: "",
      type: "",
      priority: "",
      read: "",
      q: "",
    },
    onReset: () => setCurrentOffset(0),
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
  // v5.0.22 subject + reply state.
  const [composeSubject, setComposeSubject] = useState("")
  const [composeReplyParentId, setComposeReplyParentId] = useState<string | null>(
    null,
  )
  const [suggestLoading, setSuggestLoading] = useState(false)

  // Participants drive the Compose recipient dropdown only (needs the
  // BROADCAST "*" option, which is NOT an agent and is outside
  // <AgentSelect>'s contract; the hardcoded "admin" entry mirrors
  // data-store::shouldDisplayAgent so the compose UX matches the rest
  // of the dashboard).
  const [liveParticipants, setLiveParticipants] = useState<
    { agent_id: string; status?: string }[]
  >([])

  const loadParticipants = async () => {
    try {
      const data = await callMessages("POST", "/participants", {})
      const live = Array.isArray(data?.live) ? data.live : []
      setLiveParticipants(live)
    } catch {
      // Soft-fail: dropdown just shows the hardcoded admin entry.
      setLiveParticipants([])
    }
  }
  useEffect(() => {
    loadParticipants()
  }, [])

  // Compose recipient list (live-only — admin pinned, then workers).
  const recipientOptions = useMemo(() => {
    const ids = new Set<string>(["admin"])
    for (const a of liveParticipants) {
      if (a.agent_id) ids.add(a.agent_id)
    }
    return Array.from(ids)
  }, [liveParticipants])

  // Build the spread-filter slice for the POST body. Empty-string
  // fields are dropped; ``read`` is converted from its tri-state string
  // form ("" | "true" | "false") into the real boolean the API wants.
  const queryFilters = useMemo<Record<string, unknown>>(() => {
    const f: Record<string, unknown> = {}
    if (filters.from) f.from = filters.from
    if (filters.to) f.to = filters.to
    if (filters.type) f.type = filters.type
    if (filters.priority) f.priority = filters.priority
    if (filters.read !== "") f.read = filters.read === "true"
    if (filters.q) f.q = filters.q
    return f
  }, [filters])

  // v5.0.31: paginated listing fetch owned by ``usePagedQuery<T>``.
  const {
    data: messages,
    total,
    loading,
    error: queryError,
    refresh: refreshQuery,
    lastFetch,
  } = usePagedQuery<Message>({
    endpoint: "/messages/query",
    filters: queryFilters,
    limit: PAGE_SIZE,
    offset: currentOffset,
  })

  // Surface the hook's query error via the shared toast (matches
  // Agents/Tasks/Memories — no more in-page red banner).
  useEffect(() => {
    if (queryError) toastError(queryError, "Failed to load messages")
  }, [queryError])

  // Wrapper that also re-pulls participants + clears selection.
  const refresh = useCallback(() => {
    refreshQuery()
    setSelectedIds(new Set())
    void loadParticipants()
  }, [refreshQuery])

  // Live-lookup selector shared by the detail + delete dialogs — reads
  // the current row from the hook-owned ``messages`` array on every
  // render, so a mark-read PATCH (or a delete elsewhere) re-renders the
  // open dialog against fresh data.
  const messageSelector = useCallback(
    (id: string | null) =>
      id ? messages.find((m) => m.message_id === id) ?? null : null,
    [messages],
  )
  const detailDialog = useDialog<Message>(messageSelector)
  const deleteDialog = useDialog<Message>(messageSelector)
  // Bulk-delete confirm has no single message key — a boolean drives it.
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)

  // Deleted-while-open: if the row is removed from the list, the
  // selector returns null. Auto-close so the user isn't stranded.
  useEffect(() => {
    if (detailDialog.isOpen && detailDialog.data === null) detailDialog.close()
  }, [detailDialog.isOpen, detailDialog.data, detailDialog.close])
  useEffect(() => {
    if (deleteDialog.isOpen && deleteDialog.data === null) deleteDialog.close()
  }, [deleteDialog.isOpen, deleteDialog.data, deleteDialog.close])

  // v5.0.24 polish: human-readable label for a parent message id.
  const labelForParent = useCallback(
    (parentId: string | null): string => {
      if (!parentId) return ""
      const parent = messages.find((m) => m.message_id === parentId)
      if (parent) {
        if (parent.subject && parent.subject.trim()) return parent.subject
        const snippet = (parent.message_content || "").replace(/\s+/g, " ").trim()
        if (snippet) {
          return snippet.length > 40 ? snippet.slice(0, 40) + "…" : snippet
        }
      }
      return parentId
    },
    [messages],
  )

  const send = async () => {
    if (!composeRecipient || !composeContent) return
    setComposing(true)
    try {
      // BROADCAST sentinel maps to recipient_id="*" on the backend.
      const recipient =
        composeRecipient === BROADCAST ? "*" : composeRecipient
      const body: Record<string, unknown> = {
        recipient_id: recipient,
        message_content: composeContent,
        message_type: composeType,
        priority: composePriority,
      }
      if (composeReplyParentId) {
        body.parent_message_id = composeReplyParentId
      } else if (composeSubject.trim()) {
        body.subject = composeSubject.trim()
      }
      await callMessages("POST", "", body)
      setComposeContent("")
      setComposeSubject("")
      setComposeReplyParentId(null)
      setComposeOpen(false)
      refresh()
      toastSuccess("Message sent.")
    } catch (e) {
      toastError(e, "Failed to send message")
    } finally {
      setComposing(false)
    }
  }

  // v5.0.22: ask the backend (which delegates to Ollama if
  // AGENT_MCP_SUBJECT_MODEL is configured) to propose a subject.
  const [suggestHint, setSuggestHint] = useState<string | null>(null)
  const suggestSubject = async () => {
    if (!composeContent.trim()) return
    setSuggestLoading(true)
    setSuggestHint(null)
    try {
      const data = await callMessages("POST", "/suggest-subject", {
        content: composeContent,
      })
      if (data?.subject) {
        setComposeSubject(String(data.subject))
      } else {
        setSuggestHint(
          "No suggestion available — type a subject manually " +
            "(or set AGENT_MCP_SUBJECT_MODEL server-side to enable Ollama).",
        )
      }
    } catch (e) {
      // Soft-fail — the user can still type a subject manually.
      toastError(e, "Failed to suggest a subject")
    } finally {
      setSuggestLoading(false)
    }
  }

  // Open the compose form pre-wired for a reply to the given message.
  const openReply = (parent: Message) => {
    const me = "admin" // dashboard runs as admin per ADR-0003
    const otherParty =
      parent.sender_id === me ? parent.recipient_id : parent.sender_id
    setComposeRecipient(otherParty)
    setComposeSubject("")
    setComposeContent("")
    setComposeReplyParentId(parent.message_id)
    setComposeOpen(true)
  }

  const toggleRead = async (m: Message) => {
    const nextRead = !(m.read === 1 || m.read === true)
    try {
      await callMessages("PATCH", `/${m.message_id}`, { read: nextRead })
      // Live-lookup useDialog: refreshing the list propagates the new
      // read state into the open modal automatically (modal stays open).
      refresh()
      toastSuccess(nextRead ? "Marked as read." : "Marked as unread.")
    } catch (e) {
      toastError(e, "Failed to update message")
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
    if (allVisibleSelected) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(messages.map((m) => m.message_id)))
    }
  }

  const bulkMark = async (read: boolean) => {
    if (selectedIds.size === 0) return
    const n = selectedIds.size
    try {
      await Promise.all(
        Array.from(selectedIds).map((id) =>
          callMessages("PATCH", `/${id}`, { read })
        )
      )
      refresh()
      toastSuccess(`Marked ${n} message${n === 1 ? "" : "s"} as ${read ? "read" : "unread"}.`)
    } catch (e) {
      toastError(e, "Failed to update messages")
    }
  }

  // Confirmed single delete — the onConfirm handler for the
  // DeleteMessageModal opened from a row / mobile row / the detail
  // modal's Delete button. Re-throws on failure so the confirm dialog
  // stays open (mirrors memories' handleDeleteMemory).
  const handleConfirmDelete = async () => {
    const m = deleteDialog.data
    if (!m) return
    try {
      await callMessages("DELETE", `/${m.message_id}`, {})
      refresh()
      toastSuccess("Message deleted.")
    } catch (e) {
      toastError(e, "Failed to delete message")
      throw e
    }
  }

  // Confirmed bulk delete — onConfirm for the bulk DeleteMessageModal.
  const handleConfirmBulkDelete = async () => {
    const ids = Array.from(selectedIds)
    if (ids.length === 0) return
    try {
      await Promise.all(ids.map((id) => callMessages("DELETE", `/${id}`, {})))
      refresh()
      toastSuccess(`${ids.length} message${ids.length === 1 ? "" : "s"} deleted.`)
    } catch (e) {
      toastError(e, "Failed to delete messages")
      throw e
    }
  }

  // v5.0.26 pagination handlers.
  const goNewest = () => setCurrentOffset(0)
  const goNewer = () =>
    setCurrentOffset(Math.max(0, currentOffset - PAGE_SIZE))
  const goOlder = () => setCurrentOffset(currentOffset + PAGE_SIZE)
  const goOldest = () =>
    setCurrentOffset(Math.floor(Math.max(0, total - 1) / PAGE_SIZE) * PAGE_SIZE)

  const onFirstPage = currentOffset === 0
  const onLastPage = currentOffset + PAGE_SIZE >= total
  const rangeStart = total === 0 ? 0 : currentOffset + 1
  const rangeEnd = Math.min(currentOffset + PAGE_SIZE, total)

  // Page-scoped read/unread counts for the stats row. Total is the
  // server-reported global count; unread/read are honestly labelled
  // "on this page" since the list is paginated (100/page).
  const unreadOnPage = messages.filter(
    (m) => !(m.read === 1 || m.read === true),
  ).length
  const readOnPage = messages.length - unreadOnPage

  return (
    <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-foreground">Messages</h1>
          <p className="text-muted-foreground text-sm sm:text-base mt-1">Inspect and route inter-agent messages</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          {activeServer && (
            <Badge variant="outline" className="text-xs bg-primary/15 text-primary border-primary/30 font-medium">
              <span aria-hidden className="w-2 h-2 bg-primary rounded-full mr-2" />
              {activeServer.name}
            </Badge>
          )}
          {lastFetch && (
            <span className="text-xs text-muted-foreground">
              Last updated: {new Date(lastFetch).toLocaleTimeString()}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={refresh}
            disabled={loading}
            className="text-xs"
          >
            <RefreshCw className={cn("h-3.5 w-3.5 mr-1.5", loading && "animate-spin")} />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setComposeOpen((v) => !v)}>
            {composeOpen ? (
              <>
                <X className="h-4 w-4 mr-1" />
                Close
              </>
            ) : (
              <>
                <Plus className="h-4 w-4 mr-1" />
                New Message
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid gap-3 sm:gap-4 grid-cols-2 xl:grid-cols-4">
        <StatsCard
          icon={MessageSquare}
          label="Total"
          value={total}
          change={total > 0 ? `${messages.length} on this page` : undefined}
          trend="neutral"
        />
        <StatsCard
          icon={Mail}
          label="Unread"
          value={unreadOnPage}
          change="on this page"
          trend={unreadOnPage > 0 ? "down" : "neutral"}
        />
        <StatsCard
          icon={MailOpen}
          label="Read"
          value={readOnPage}
          change="on this page"
          trend="up"
        />
        <StatsCard
          icon={CheckSquare}
          label="Selected"
          value={selectedIds.size}
          change={selectedIds.size > 0 ? "ready to act" : "none"}
          trend="neutral"
        />
      </div>

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
            {/* v5.0.22: Subject input + Suggest button. Hidden when
                replying — replies always have subject = NULL per the
                schema contract. */}
            {composeReplyParentId ? (
              <div className="rounded-md border border-muted bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                ↳ reply to:{" "}
                <span className="font-medium text-foreground">
                  {labelForParent(composeReplyParentId)}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="ml-2 h-6 px-2"
                  onClick={() => setComposeReplyParentId(null)}
                >
                  Cancel reply
                </Button>
              </div>
            ) : (
              <div>
                <Label className="text-xs">Subject</Label>
                <div className="flex gap-2">
                  <Input
                    placeholder="Subject (optional — Suggest will fill from Ollama)"
                    value={composeSubject}
                    onChange={(e) => {
                      setComposeSubject(e.target.value)
                      if (suggestHint) setSuggestHint(null)
                    }}
                  />
                  <Button
                    type="button"
                    variant="outline"
                    onClick={suggestSubject}
                    disabled={suggestLoading || !composeContent.trim()}
                    title="Ask the configured Ollama model for a subject — POST /api/messages/suggest-subject"
                  >
                    {suggestLoading ? "…" : "Suggest"}
                  </Button>
                </div>
                {suggestHint && (
                  <p className="text-[11px] text-muted-foreground mt-1">
                    {suggestHint}
                  </p>
                )}
              </div>
            )}
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
                <Send className="h-4 w-4 mr-1" />
                {composing ? "Sending…" : "Send"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Controls — un-boxed filter bar matching Memories/Tasks (no
          Card wrapper). */}
      <div className="flex flex-col sm:flex-row sm:flex-wrap items-stretch sm:items-center gap-2 sm:gap-3">
        <div className="relative flex-1 sm:max-w-xs">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search content..."
            value={filters.q}
            onChange={(e) => setFilter("q", e.target.value)}
            className="pl-10"
          />
        </div>
        {/*
          From/To filter dropdowns share <AgentSelect> with every other
          agent-input site. noneLabel="— Any —" because an empty filter
          means "no filter".
        */}
        <div className="w-full sm:w-40">
          <AgentSelect
            value={filters.from || null}
            onChange={(v) => setFilter("from", v ?? "")}
            noneLabel="— Any —"
            placeholder="from"
          />
        </div>
        <div className="w-full sm:w-40">
          <AgentSelect
            value={filters.to || null}
            onChange={(v) => setFilter("to", v ?? "")}
            noneLabel="— Any —"
            placeholder="to"
          />
        </div>
        <Select
          value={filters.type || ALL}
          onValueChange={(v) => setFilter("type", v === ALL ? "" : v)}
        >
          <SelectTrigger className="w-full sm:w-40"><SelectValue placeholder="type" /></SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>all types</SelectItem>
            {MESSAGE_TYPES.map((t) => (<SelectItem key={t} value={t}>{t}</SelectItem>))}
          </SelectContent>
        </Select>
        <Select
          value={filters.priority || ALL}
          onValueChange={(v) => setFilter("priority", v === ALL ? "" : v)}
        >
          <SelectTrigger className="w-full sm:w-36"><SelectValue placeholder="priority" /></SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>any priority</SelectItem>
            {PRIORITIES.map((p) => (<SelectItem key={p} value={p}>{p}</SelectItem>))}
          </SelectContent>
        </Select>
        <Select
          value={filters.read || ALL}
          onValueChange={(v) => setFilter("read", v === ALL ? "" : (v as "true" | "false"))}
        >
          <SelectTrigger className="w-full sm:w-32"><SelectValue placeholder="read?" /></SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>any</SelectItem>
            <SelectItem value="false">unread</SelectItem>
            <SelectItem value="true">read</SelectItem>
          </SelectContent>
        </Select>
        <Button variant="ghost" size="sm" onClick={clearFilters}>
          <X className="h-4 w-4 mr-1" />
          Clear
        </Button>
      </div>

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
              <Button variant="destructive" size="sm" onClick={() => setBulkDeleteOpen(true)}>
                <Trash2 className="h-4 w-4 mr-1" />
                Delete
              </Button>
            </div>
          )}
        </CardHeader>
        <CardContent>
          {loading && messages.length === 0 ? (
            <div className="space-y-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          ) : messages.length === 0 ? (
            <EmptyState
              icon={MessageSquare}
              title="No messages"
              description="No messages match the current filters."
              action={
                <Button variant="outline" size="sm" onClick={clearFilters}>
                  <X className="h-4 w-4 mr-1" />
                  Clear filters
                </Button>
              }
            />
          ) : (
            <>
              {/* Desktop table */}
              <div className="hidden sm:block overflow-x-auto">
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
                      <TableHead>Subject</TableHead>
                      <TableHead>Type</TableHead>
                      <TableHead>Priority</TableHead>
                      <TableHead>Read?</TableHead>
                      <TableHead>Content</TableHead>
                      <TableHead className="w-8"></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {messages.map((m) => (
                      <MessageRow
                        key={m.message_id}
                        message={m}
                        selected={selectedIds.has(m.message_id)}
                        onToggle={toggleOne}
                        onOpenDetail={(msg) => detailDialog.open(msg.message_id)}
                        onDelete={(msg) => deleteDialog.open(msg.message_id)}
                        labelForParent={labelForParent}
                      />
                    ))}
                  </TableBody>
                </Table>
              </div>
              {/* Mobile card-list (CC-7) */}
              <div className="block sm:hidden -m-6">
                <MessagesMobileList
                  messages={messages}
                  selectedIds={selectedIds}
                  toggleOne={toggleOne}
                  openDetail={(m) => detailDialog.open(m.message_id)}
                  deleteOne={(m) => deleteDialog.open(m.message_id)}
                  labelForParent={labelForParent}
                  currentOffset={currentOffset}
                  total={total}
                  pageSize={PAGE_SIZE}
                  onNewest={goNewest}
                  onNewer={goNewer}
                  onOlder={goOlder}
                  onOldest={goOldest}
                />
              </div>
            </>
          )}
          {/* v5.0.26: pagination footer — desktop only. */}
          {total > 0 && (
            <div className="hidden sm:flex items-center justify-between gap-2 mt-4 pt-3 border-t">
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={goNewest}
                  disabled={onFirstPage}
                  aria-label="jump to newest page"
                >
                  « Newest
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={goNewer}
                  disabled={onFirstPage}
                >
                  Newer
                </Button>
              </div>
              <div className="text-xs text-muted-foreground tabular-nums">
                Showing {rangeStart}–{rangeEnd} of {total}
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={goOlder}
                  disabled={onLastPage}
                >
                  Older
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={goOldest}
                  disabled={onLastPage}
                  aria-label="jump to oldest page"
                >
                  Oldest »
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Detail modal (extracted <ViewMessageModal>). Mark-read stays
          inline (modal stays open); Delete routes through the confirm
          dialog below. */}
      <ViewMessageModal
        message={detailDialog.data}
        open={detailDialog.isOpen}
        onOpenChange={(open) => { if (!open) detailDialog.close() }}
        onReply={() => {
          const m = detailDialog.data
          if (!m) return
          openReply(m)
          detailDialog.close()
        }}
        onToggleRead={() => {
          const m = detailDialog.data
          if (m) void toggleRead(m)
        }}
        onDelete={() => {
          const m = detailDialog.data
          if (!m) return
          detailDialog.close()
          deleteDialog.open(m.message_id)
        }}
      />

      {/* Single-message delete confirmation (type-DELETE-to-confirm). */}
      <DeleteMessageModal
        message={deleteDialog.data}
        open={deleteDialog.isOpen}
        onOpenChange={(open) => { if (!open) deleteDialog.close() }}
        onConfirm={handleConfirmDelete}
      />

      {/* Bulk delete confirmation. */}
      <DeleteMessageModal
        count={selectedIds.size}
        open={bulkDeleteOpen}
        onOpenChange={setBulkDeleteOpen}
        onConfirm={handleConfirmBulkDelete}
      />
    </div>
  )
}
