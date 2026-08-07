"use client"

import React, { useCallback, useEffect, useMemo, useState } from "react"
import {
  MessageSquare,
  Send,
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
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { MessageMobileCard } from "@/components/dashboard/messages-mobile-list"
import {
  priorityBadgeClass,
  messageTypeBadgeClass,
} from "@/components/dashboard/shared/message-badges"
import { ViewMessageModal } from "@/components/dashboard/modals/view-message-modal"
import { DeleteConfirmModal } from "@/components/dashboard/modals/delete-confirm-modal"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { DataTablePage } from "@/components/dashboard/shared/data-table-page"
import type { EmptyStateProps } from "@/components/dashboard/shared/empty-state"
import type { StatsCardProps } from "@/components/dashboard/shared/stats-card"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"

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

// Background-refresh interval so new inbound messages appear without a
// manual Refresh (mirrors tasks-dashboard's REFRESH_INTERVAL). The
// refresh re-runs the paged query in place — it does NOT reset the
// cursor (currentOffset) or the filters, so the user's page/scroll is
// preserved. Paused while the compose form is open so a background
// refresh can't disrupt an in-progress draft.
const REFRESH_INTERVAL = 60000 // 1 minute

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

// A filter control with a small visible label stacked above it, so each
// dropdown is self-describing before it's opened (the From/To agent
// pickers otherwise both just read "— Any —").
const FilterField = ({
  label,
  className,
  children,
}: {
  label: string
  className?: string
  children: React.ReactNode
}) => (
  <div className={cn("flex flex-col gap-1", className)}>
    <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
      {label}
    </span>
    {children}
  </div>
)

// Preview block shown inside the DeleteConfirmModal `details` slot for
// the SINGLE-message variant — reproduces the pre-foundation
// DeleteMessageModal body (participants / SUBJECT / CONTENT PREVIEW /
// metadata). The bulk variant has no preview (there is no single row to
// show); it overrides `title` / `description` / `warningText` instead.
function MessageDeletePreview({ message }: { message: Message }) {
  const formatContent = (value: string) =>
    value.length > 120 ? value.substring(0, 120) + "…" : value
  return (
    <div className="space-y-3">
      <div className="text-sm font-medium text-foreground">Message to be deleted:</div>
      <div className="bg-muted/30 border border-border rounded-lg p-3 space-y-3">
        {/* Participants */}
        <div className="flex items-center gap-2 text-sm">
          <Badge variant="outline">{message.sender_id}</Badge>
          <span aria-hidden className="text-muted-foreground">→</span>
          <Badge variant="outline">{message.recipient_id}</Badge>
        </div>
        {message.subject && (
          <div>
            <div className="text-xs font-medium text-muted-foreground mb-1">SUBJECT</div>
            <div className="text-sm text-foreground">{message.subject}</div>
          </div>
        )}
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-1">CONTENT PREVIEW</div>
          <div className="text-sm text-muted-foreground bg-background border border-border rounded px-2 py-1 font-mono max-h-16 overflow-hidden">
            {formatContent(message.message_content)}
          </div>
        </div>
        <div className="flex items-center gap-4 text-xs text-muted-foreground pt-2 border-t border-border">
          <span>{message.timestamp.slice(0, 19)}</span>
          <span>Type: {message.message_type}</span>
          <span>Priority: {message.priority}</span>
        </div>
      </div>
    </div>
  )
}

/**
 * v5.0.26 pagination footer — « Newest / Newer / Older / Oldest » plus
 * a "Showing N–M of T" range label.
 *
 * One button spec drives both layouts: a single justified row on sm+,
 * and (below sm) the range label stacked above a 4-column grid so the
 * labels stay readable at 375 px. Pre-scaffold these were two separate
 * hand-written copies — one here, one inside messages-mobile-list.tsx —
 * which is the same double-renderer drift the shared table retires.
 */
function MessagesPagination({
  rangeStart,
  rangeEnd,
  total,
  onFirstPage,
  onLastPage,
  onNewest,
  onNewer,
  onOlder,
  onOldest,
}: {
  rangeStart: number
  rangeEnd: number
  total: number
  onFirstPage: boolean
  onLastPage: boolean
  onNewest: () => void
  onNewer: () => void
  onOlder: () => void
  onOldest: () => void
}) {
  const nav = [
    {
      key: "newest",
      label: "« Newest",
      onClick: onNewest,
      disabled: onFirstPage,
      ariaLabel: "jump to newest page",
    },
    { key: "newer", label: "Newer", onClick: onNewer, disabled: onFirstPage },
    { key: "older", label: "Older", onClick: onOlder, disabled: onLastPage },
    {
      key: "oldest",
      label: "Oldest »",
      onClick: onOldest,
      disabled: onLastPage,
      ariaLabel: "jump to oldest page",
    },
  ]
  const button = (b: (typeof nav)[number]) => (
    <Button
      key={b.key}
      variant="outline"
      size="sm"
      onClick={b.onClick}
      disabled={b.disabled}
      aria-label={b.ariaLabel}
    >
      {b.label}
    </Button>
  )
  const range = `Showing ${rangeStart}–${rangeEnd} of ${total}`
  return (
    <>
      <div className="hidden sm:flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">{nav.slice(0, 2).map(button)}</div>
        <div className="text-xs text-muted-foreground tabular-nums">{range}</div>
        <div className="flex items-center gap-2">{nav.slice(2).map(button)}</div>
      </div>
      <div className="block sm:hidden">
        <div className="text-[11px] text-muted-foreground tabular-nums text-center mb-2">
          {range}
        </div>
        <div className="grid grid-cols-4 gap-2">{nav.map(button)}</div>
      </div>
    </>
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
  // feat/reply-as-recipient: when replying, the operator answers AS the
  // parent message's recipient (e.g. "manager"), back to its sender. This
  // holds the reply-as identity; null for a fresh compose or when the
  // operator is replying as themselves (admin — the normal case, which
  // sends no sender_id override).
  const [composeReplyAs, setComposeReplyAs] = useState<string | null>(null)
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
  // The currently-selected recipient is always appended if it isn't a
  // live participant: openReply() can target an agent that has since
  // gone offline, and a Radix Select with a value that has no matching
  // <SelectItem> renders a blank trigger. Keeping the selected id in the
  // list guarantees the value always renders. BROADCAST is its own
  // hardcoded item, so it's excluded here.
  const recipientOptions = useMemo(() => {
    const ids = new Set<string>(["admin"])
    for (const a of liveParticipants) {
      if (a.agent_id) ids.add(a.agent_id)
    }
    if (composeRecipient && composeRecipient !== BROADCAST) {
      ids.add(composeRecipient)
    }
    return Array.from(ids)
  }, [liveParticipants, composeRecipient])

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
  //
  // NOTE: this is deliberately NOT wired into <DataTablePage>'s
  // scaffold-owned `error` panel. That panel REPLACES the page, and a
  // messages list refreshes itself every 60 s / on every SSE tick — one
  // transient failure would blank a page the operator is reading. The
  // toast is the pre-existing (and still correct) surface here;
  // switching to the panel would be a UX change, not a refactor.
  useEffect(() => {
    if (queryError) toastError(queryError, "Failed to load messages")
  }, [queryError])

  // Wrapper that also re-pulls participants + clears selection.
  const refresh = useCallback(() => {
    refreshQuery()
    setSelectedIds(new Set())
    void loadParticipants()
  }, [refreshQuery])

  // Background refresh so new inbound messages surface without a manual
  // Refresh (mirrors tasks-dashboard). Calls ``refreshQuery()`` directly
  // — the in-place paged refetch at the current offset/filters — instead
  // of the ``refresh`` wrapper, so a background tick does NOT wipe the
  // user's row selection or reset their page/scroll. Paused while the
  // compose form is open so it can't disrupt an in-progress draft.
  useEffect(() => {
    if (composeOpen) return
    const interval = setInterval(() => {
      refreshQuery()
    }, REFRESH_INTERVAL)
    return () => clearInterval(interval)
  }, [refreshQuery, composeOpen])

  // Live refetch on backend mutation. The operator SSE client
  // (lib/mcp-notifications.ts) dispatches a debounced
  // ``mcp:resources-updated`` window event on every
  // ``notifications/resources/updated``. The messages list polls its
  // OWN endpoint (POST /messages/query), so the data-store's
  // scheduleDashboardRefresh doesn't cover it — hook the event to the
  // in-place paged refetch (no local debounce: backend + client already
  // debounce). Paused while composing, mirroring the background-refresh
  // pause so a live tick can't disrupt an in-progress draft.
  useEffect(() => {
    if (typeof window === "undefined" || composeOpen) return
    const handler = () => {
      refreshQuery()
    }
    window.addEventListener("mcp:resources-updated", handler)
    return () => window.removeEventListener("mcp:resources-updated", handler)
  }, [refreshQuery, composeOpen])

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
      // feat/reply-as-recipient: when replying AS an agent (not the
      // operator's own identity), override the stored sender so the reply
      // is authored in that agent's voice. The backend validates + audits
      // this (operator-only). Omitted for a normal send / reply-as-admin.
      if (composeReplyAs) {
        body.sender_id = composeReplyAs
      }
      await callMessages("POST", "", body)
      setComposeContent("")
      setComposeSubject("")
      setComposeReplyParentId(null)
      setComposeReplyAs(null)
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
  //
  // feat/reply-as-recipient: a reply is the message's RECIPIENT answering
  // its SENDER. So we reply AS `parent.recipient_id` (`replyAs`) and send
  // back TO `parent.sender_id` (`replyTo`). Example: a message
  // `backend-dev → manager` yields "Reply as manager" sending
  // `manager → backend-dev`.
  //
  // A listed row carries a concrete per-recipient `recipient_id` (the
  // broadcast fan-out is stored per recipient), so `replyAs` is a real
  // agent. Guard the degenerate broadcast token "*"/empty: fall back to
  // the old behavior (reply to the other party as the operator) rather
  // than compose a message authored by "*".
  const openReply = (parent: Message) => {
    const me = "admin" // dashboard runs as admin per ADR-0003
    const replyAs = parent.recipient_id
    const replyTo = parent.sender_id
    const broadcastLike = !replyAs || replyAs === "*"
    if (broadcastLike) {
      // Degenerate: no concrete recipient to speak as. Reply to the other
      // party as the operator (legacy behavior), no sender override.
      const otherParty = parent.sender_id === me ? parent.recipient_id : parent.sender_id
      setComposeRecipient(otherParty)
      setComposeReplyAs(null)
    } else {
      setComposeRecipient(replyTo)
      // Only carry an override when actually acting AS an agent (i.e. the
      // reply-as identity is not the operator's own identity). Replying as
      // admin is the normal operator-replying-as-themselves case.
      setComposeReplyAs(replyAs === me ? null : replyAs)
    }
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
  // DeleteConfirmModal opened from a row / mobile card / the detail
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

  // Confirmed bulk delete — onConfirm for the bulk DeleteConfirmModal.
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

  // True when any filter is actually set — drives the empty-state copy
  // (a real "no rows for these filters" vs a plain "no messages yet").
  const hasActiveFilters = useMemo(
    () => Object.values(filters).some((v) => v !== ""),
    [filters],
  )

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

  // Stats strip — rendered by the scaffold's shared <StatsCard> row
  // (the page's own copy of that component is retired).
  const statsCards: StatsCardProps[] = [
    {
      icon: MessageSquare,
      label: "Total",
      value: total,
      change: total > 0 ? `${messages.length} on this page` : undefined,
      trend: "neutral",
    },
    {
      icon: Mail,
      label: "Unread",
      value: unreadOnPage,
      change: "on this page",
      trend: unreadOnPage > 0 ? "down" : "neutral",
    },
    {
      icon: MailOpen,
      label: "Read",
      value: readOnPage,
      change: "on this page",
      trend: "up",
    },
    {
      icon: CheckSquare,
      label: "Selected",
      value: selectedIds.size,
      change: selectedIds.size > 0 ? "ready to act" : "none",
      trend: "neutral",
    },
  ]

  // Empty-state copy branches on whether a filter is actually set: a
  // filtered-to-nothing list gets a Clear-filters CTA, an untouched
  // empty inbox just says so. Rendered by the scaffold's shared
  // <EmptyState> (CC-20).
  const emptyState: EmptyStateProps = hasActiveFilters ? ({
    icon: MessageSquare,
    title: "No messages",
    description: "No messages match the current filters.",
    action: (
      <Button variant="outline" size="sm" onClick={clearFilters}>
        <X className="h-4 w-4 mr-1" />
        Clear filters
      </Button>
    ),
  }) : ({
    // No filters active → nothing to clear; just an empty inbox.
    icon: MessageSquare,
    title: "No messages yet",
    description: "No messages have been sent yet.",
  })

  // Column spec — ONE source for the desktop table (via
  // <ResponsiveDataTable>) and, through `renderMobileCard`, the mobile
  // card. Cells reproduce the pre-foundation <MessageRow> exactly; the
  // checkbox + delete cells stopPropagation so they don't also fire the
  // row-body onClick (open detail).
  const columns: Column<Message>[] = [
    {
      id: "select",
      headClassName: "w-8",
      header: (
        <input
          type="checkbox"
          aria-label="select all visible"
          checked={allVisibleSelected}
          onChange={toggleAllVisible}
        />
      ),
      cell: (m) => (
        <input
          type="checkbox"
          aria-label={`select message ${m.message_id}`}
          checked={selectedIds.has(m.message_id)}
          onChange={() => toggleOne(m.message_id)}
          onClick={(e) => e.stopPropagation()}
        />
      ),
    },
    {
      id: "time",
      header: "Time",
      cellClassName: "text-xs font-mono tabular-nums",
      // Per-row entity glyph (matches memories' <Brain> convention).
      cell: (m) => (
        <div className="flex items-center gap-2">
          <MessageSquare className="h-3 w-3 text-primary flex-shrink-0" />
          <span>{m.timestamp.slice(0, 19)}</span>
        </div>
      ),
    },
    {
      id: "from",
      header: "From",
      cellClassName: "max-w-[160px]",
      cell: (m) => {
        const isRead = m.read === 1 || m.read === true
        return (
          <div className="flex items-center gap-1.5">
            {/* Leading unread dot — mirrors the mobile treatment so an
                unread row is scannable at a glance, not just a ✓ column. */}
            {!isRead && (
              <span
                aria-hidden
                className="h-2 w-2 flex-shrink-0 rounded-full bg-primary"
              />
            )}
            {/* Long agent ids truncate (with a title tooltip) instead of
                growing the column and forcing table-wide horizontal
                overflow. */}
            <Badge
              variant="outline"
              className={cn("min-w-0 max-w-full", !isRead && "font-semibold")}
              title={m.sender_id}
            >
              <span className="truncate">{m.sender_id}</span>
            </Badge>
          </div>
        )
      },
    },
    {
      id: "to",
      header: "To",
      cellClassName: "max-w-[160px]",
      cell: (m) => (
        <Badge
          variant="outline"
          className="min-w-0 max-w-full"
          title={m.recipient_id}
        >
          <span className="truncate">{m.recipient_id}</span>
        </Badge>
      ),
    },
    {
      id: "subject",
      header: "Subject",
      cellClassName: "text-xs max-w-[200px] truncate",
      cell: (m) => {
        const isRead = m.read === 1 || m.read === true
        const isReply = !!m.parent_message_id
        return (
          <span className={cn(!isRead && "font-semibold text-foreground")}>
            {m.subject && m.subject_is_placeholder ? (
              // Placeholder: the sender set no subject, so this is an
              // auto-preview of the body (Phase 1). Shown muted + italic
              // with an "auto" tag so it reads as a stub, not a real
              // subject — a generated one fills in on the next backfill
              // sweep (Phase 2).
              <span
                className="italic text-muted-foreground"
                title="No subject set — auto-preview of the message. A generated subject will fill in shortly."
              >
                {m.subject}
                <span className="ml-1 not-italic text-[10px] font-medium uppercase tracking-wide text-muted-foreground/60">
                  auto
                </span>
              </span>
            ) : m.subject ? (
              // Real subject: title reveals the full text on hover when the
              // cell truncates. (The placeholder branch keeps its own
              // explanatory title, so we don't clobber it here.)
              <span title={m.subject}>{m.subject}</span>
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
          </span>
        )
      },
    },
    {
      id: "type",
      header: "Type",
      cellClassName: "text-xs",
      cell: (m) => (
        <Badge variant="outline" className={messageTypeBadgeClass(m.message_type)}>
          {m.message_type}
        </Badge>
      ),
    },
    {
      id: "priority",
      header: "Priority",
      cellClassName: "text-xs",
      cell: (m) => (
        <Badge variant="outline" className={priorityBadgeClass(m.priority)}>
          {m.priority}
        </Badge>
      ),
    },
    {
      id: "read",
      header: "Read?",
      // Glyph is silent to screen readers; the sr-only text names the
      // state so it's announced.
      cell: (m) => {
        const isRead = m.read === 1 || m.read === true
        return (
          <>
            <span aria-hidden>{isRead ? "✓" : ""}</span>
            <span className="sr-only">{isRead ? "read" : "unread"}</span>
          </>
        )
      },
    },
    {
      id: "content",
      header: "Content",
      cellClassName: "max-w-[400px] truncate text-xs",
      cell: (m) => {
        const isRead = m.read === 1 || m.read === true
        return (
          <span
            className={cn(!isRead && "text-foreground")}
            title={m.message_content}
          >
            {m.message_content}
          </span>
        )
      },
    },
    {
      id: "actions",
      header: "",
      headClassName: "w-8",
      cell: (m) => (
        <Button
          variant="ghost"
          size="sm"
          aria-label="delete message"
          className="text-destructive hover:text-destructive hover:bg-destructive/10"
          onClick={(e) => { e.stopPropagation(); deleteDialog.open(m.message_id) }}
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      ),
    },
  ]

  // Everything between the stats strip and the table card. The scaffold
  // exposes ONE slot there (`filterBar`), and the compose panel + the
  // selection toolbar both belong in that band:
  //   * Compose must stay ABOVE the list — pushing it into `children`
  //     (below the table) would hide a freshly-opened draft under 100
  //     rows.
  //   * The selection toolbar ("N messages / N selected" + bulk
  //     actions) was the table Card's CardHeader; the scaffold owns the
  //     card and has no header slot, so it sits directly above it.
  const filterBar = (
    <div className="w-full space-y-4 sm:space-y-6">
      {composeOpen && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Compose message</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-3 md:grid-cols-3">
              <div>
                <Label htmlFor="compose-recipient" className="text-xs">Recipient agent_id</Label>
                <Select
                  value={composeRecipient}
                  onValueChange={setComposeRecipient}
                >
                  <SelectTrigger id="compose-recipient" aria-label="Recipient agent_id">
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
                <Label htmlFor="compose-type" className="text-xs">Type</Label>
                <Select value={composeType} onValueChange={setComposeType}>
                  <SelectTrigger id="compose-type" aria-label="Message type"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {MESSAGE_TYPES.map((t) => (
                      <SelectItem key={t} value={t}>{t}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label htmlFor="compose-priority" className="text-xs">Priority</Label>
                <Select value={composePriority} onValueChange={setComposePriority}>
                  <SelectTrigger id="compose-priority" aria-label="Priority"><SelectValue /></SelectTrigger>
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
                {/* feat/reply-as-recipient: make the operator's voice
                    explicit — they are replying AS the parent's recipient,
                    back TO its sender. Shown only when acting as an agent
                    (composeReplyAs set); a plain reply-as-admin keeps the
                    ordinary "reply to" line. */}
                {composeReplyAs ? (
                  <div className="mb-1 font-medium text-foreground">
                    Replying as {composeReplyAs} → {composeRecipient}
                  </div>
                ) : null}
                ↳ reply to:{" "}
                <span className="font-medium text-foreground">
                  {labelForParent(composeReplyParentId)}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="ml-2 h-6 px-2"
                  onClick={() => {
                    setComposeReplyParentId(null)
                    setComposeReplyAs(null)
                  }}
                >
                  Cancel reply
                </Button>
              </div>
            ) : (
              <div>
                <Label htmlFor="compose-subject" className="text-xs">Subject</Label>
                <div className="flex gap-2">
                  <Input
                    id="compose-subject"
                    aria-label="Subject"
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
              <Label htmlFor="compose-content" className="text-xs">Content</Label>
              <textarea
                id="compose-content"
                aria-label="Content"
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
          Card wrapper). Each control carries a small visible label above
          it so the filters are self-describing before you open them
          (two are agent pickers that otherwise both just read "— Any —").
          A `<FilterField>` wraps each in a label + control stack. */}
      <div className="flex flex-col sm:flex-row sm:flex-wrap items-stretch sm:items-end gap-2 sm:gap-3">
        <FilterField label="Search" className="flex-1 sm:max-w-xs">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              aria-label="Search messages"
              placeholder="subject, sender, recipient, content…"
              value={filters.q}
              onChange={(e) => setFilter("q", e.target.value)}
              className="pl-10"
            />
          </div>
        </FilterField>
        {/*
          From/To filter dropdowns share <AgentSelect> with every other
          agent-input site. noneLabel="— Any —" because an empty filter
          means "no filter".
        */}
        <FilterField label="From" className="w-full sm:w-40">
          <AgentSelect
            value={filters.from || null}
            onChange={(v) => setFilter("from", v ?? "")}
            noneLabel="— Any —"
            placeholder="from"
            ariaLabel="Filter by sender"
          />
        </FilterField>
        <FilterField label="To" className="w-full sm:w-40">
          <AgentSelect
            value={filters.to || null}
            onChange={(v) => setFilter("to", v ?? "")}
            noneLabel="— Any —"
            placeholder="to"
            ariaLabel="Filter by recipient"
          />
        </FilterField>
        <FilterField label="Type" className="w-full sm:w-40">
          <Select
            value={filters.type || ALL}
            onValueChange={(v) => setFilter("type", v === ALL ? "" : v)}
          >
            <SelectTrigger aria-label="Filter by type" className="w-full"><SelectValue placeholder="type" /></SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>all types</SelectItem>
              {MESSAGE_TYPES.map((t) => (<SelectItem key={t} value={t}>{t}</SelectItem>))}
            </SelectContent>
          </Select>
        </FilterField>
        <FilterField label="Priority" className="w-full sm:w-36">
          <Select
            value={filters.priority || ALL}
            onValueChange={(v) => setFilter("priority", v === ALL ? "" : v)}
          >
            <SelectTrigger aria-label="Filter by priority" className="w-full"><SelectValue placeholder="priority" /></SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>any priority</SelectItem>
              {PRIORITIES.map((p) => (<SelectItem key={p} value={p}>{p}</SelectItem>))}
            </SelectContent>
          </Select>
        </FilterField>
        <FilterField label="Status" className="w-full sm:w-32">
          <Select
            value={filters.read || ALL}
            onValueChange={(v) => setFilter("read", v === ALL ? "" : (v as "true" | "false"))}
          >
            <SelectTrigger aria-label="Filter by read status" className="w-full"><SelectValue placeholder="read?" /></SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>any</SelectItem>
              <SelectItem value="false">unread</SelectItem>
              <SelectItem value="true">read</SelectItem>
            </SelectContent>
          </Select>
        </FilterField>
        <Button variant="ghost" size="sm" onClick={clearFilters}>
          <X className="h-4 w-4 mr-1" />
          Clear
        </Button>
      </div>

      {/* Row count + bulk-action toolbar (was the table Card's header). */}
      <div className="flex flex-row items-center justify-between gap-2">
        <div className="text-base font-semibold text-foreground">
          {messages.length} {messages.length === 1 ? "message" : "messages"}
          {selectedIds.size > 0 && (
            <span className="ml-2 text-sm font-normal text-muted-foreground">
              ({selectedIds.size} selected)
            </span>
          )}
        </div>
        {selectedIds.size > 0 && (
          <div className="flex flex-wrap gap-2">
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
      </div>
    </div>
  )

  return (
    <DataTablePage<Message>
      loading={loading}
      header={{
        title: "Messages",
        subtitle: "Inspect and route inter-agent messages",
        serverName: activeServer?.name,
        lastUpdated: lastFetch ?? undefined,
        onRefresh: refresh,
        refreshing: loading,
        actions: (
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
        ),
      }}
      stats={statsCards}
      filterBar={filterBar}
      columns={columns}
      rows={messages}
      getRowId={(m) => m.message_id}
      onRowClick={(m) => detailDialog.open(m.message_id)}
      // v5.0.22: rows whose parent_message_id is non-null are replies.
      // Visual cue = subtle left border (the "↳ reply to: <parent>"
      // prefix in the Subject column is the other half).
      rowClassName={(m) =>
        m.parent_message_id ? "border-l-2 border-l-muted-foreground/30" : undefined
      }
      skeletonRows={6}
      renderMobileCard={(m) => (
        <MessageMobileCard
          message={m}
          selected={selectedIds.has(m.message_id)}
          toggleOne={toggleOne}
          openDetail={(msg) => detailDialog.open(msg.message_id)}
          deleteOne={(msg) => deleteDialog.open(msg.message_id)}
          labelForParent={labelForParent}
        />
      )}
      empty={emptyState}
    >
      {/* v5.0.26: pagination footer (desktop row + mobile grid). */}
      {total > 0 && (
        <MessagesPagination
          rangeStart={rangeStart}
          rangeEnd={rangeEnd}
          total={total}
          onFirstPage={onFirstPage}
          onLastPage={onLastPage}
          onNewest={goNewest}
          onNewer={goNewer}
          onOlder={goOlder}
          onOldest={goOldest}
        />
      )}

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
        onToggleRead={(msg) => { void toggleRead(msg) }}
        onDelete={() => {
          const m = detailDialog.data
          if (!m) return
          detailDialog.close()
          deleteDialog.open(m.message_id)
        }}
      />

      {/* Single-message delete confirmation (type-DELETE-to-confirm,
          shared modal). The default entityLabel copy reproduces the
          retired DeleteMessageModal's single-message wording verbatim. */}
      <DeleteConfirmModal
        open={deleteDialog.isOpen}
        onOpenChange={(open) => { if (!open) deleteDialog.close() }}
        entityLabel="Message"
        details={deleteDialog.data && <MessageDeletePreview message={deleteDialog.data} />}
        onConfirm={handleConfirmDelete}
      />

      {/* Bulk delete confirmation — same shared modal, no per-row
          `details` preview (there is no single row to preview); the
          count-aware copy comes from the title/description/warning
          overrides. */}
      <DeleteConfirmModal
        open={bulkDeleteOpen}
        onOpenChange={setBulkDeleteOpen}
        entityLabel="Message"
        inputId="bulk-confirmation"
        title={`Delete ${selectedIds.size} Messages`}
        description={`This action cannot be undone. The ${selectedIds.size} selected messages will be permanently deleted.`}
        warningText={`All ${selectedIds.size} selected messages will be permanently removed. This action cannot be reversed.`}
        confirmLabel={`Delete ${selectedIds.size} Messages`}
        onConfirm={handleConfirmBulkDelete}
      />
    </DataTablePage>
  )
}
