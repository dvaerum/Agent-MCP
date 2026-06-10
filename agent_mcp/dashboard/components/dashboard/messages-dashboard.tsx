"use client"

import React, { useCallback, useEffect, useMemo, useState } from "react"
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { apiClient, type Agent } from "@/lib/api"
import { useDialog } from "@/hooks/use-dialog"
import { useFilters } from "@/hooks/use-filters"
import { usePagedQuery } from "@/hooks/use-paged-query"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/dashboard/shared/empty-state"
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { MessagesMobileList } from "@/components/dashboard/messages-mobile-list"

// Render a relative-time hint like "5 hours ago" / "in 3 minutes" so
// admins don't have to do the timezone math themselves. Falls back to
// the raw value if it can't be parsed.
function relativeTime(iso: string): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const deltaMs = t - Date.now()
  const abs = Math.abs(deltaMs)
  const sec = Math.round(abs / 1000)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
  const sign = deltaMs >= 0 ? 1 : -1
  if (sec < 60) return rtf.format(sign * sec, "second")
  const min = Math.round(sec / 60)
  if (min < 60) return rtf.format(sign * min, "minute")
  const hr = Math.round(min / 60)
  if (hr < 24) return rtf.format(sign * hr, "hour")
  const day = Math.round(hr / 24)
  if (day < 30) return rtf.format(sign * day, "day")
  const month = Math.round(day / 30)
  if (month < 12) return rtf.format(sign * month, "month")
  return rtf.format(sign * Math.round(month / 12), "year")
}

// Message row shape returned by POST /api/messages/query.
// v5.0.22: subject (root-only) + parent_message_id (NULL for roots,
// reply→root.message_id for replies).
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
  subject: string | null
  parent_message_id: string | null
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

// v5.0.26: pagination footer on the messages list. Per-page size stays
// at 100 (Dennis explicitly does not want bigger pages — see plan
// prancy-napping-pie.md). The footer adds « Newest / Newer / Older /
// Oldest » cursor buttons + a "Showing N–M of T" range label so admins
// can reach messages past the first 100 rows. Component state only —
// no URL state, matches the existing filter behavior.
const PAGE_SIZE = 100

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
    headers: {
      "Content-Type": "application/json",
      // PR-A: REST endpoints require the strict v1 media type.
      "Accept": "application/vnd.agent-mcp.v1+json",
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => "")
    throw new Error(txt || `HTTP ${res.status}`)
  }
  return res.json()
}

export function MessagesDashboard() {
  // v5.0.31: ``messages`` / ``total`` / ``loading`` / data-side
  // ``error`` are now owned by ``usePagedQuery<Message>`` (PR 5 of the
  // 2026-06-09 architecture review). The hook subsumes the bespoke
  // ``callMessages POST query`` listing fetch + the four
  // ``useState`` slots that used to mirror its result. We keep a
  // separate ``actionError`` slot below for compose / mark-read /
  // delete failures, since those code paths talk to other endpoints
  // (``POST /api/messages``, ``PATCH /api/messages/<id>``,
  // ``DELETE /api/messages/<id>``) and aren't the hook's
  // responsibility.
  const [actionError, setActionError] = useState<string | null>(null)

  // v5.0.26: pagination cursor. Total comes from the hook. Declared
  // before `filters` because the useFilters() `onReset` callback below
  // closes over `setCurrentOffset` — the legacy
  // `useEffect(() => setCurrentOffset(0), [filters])` block is gone;
  // useFilters fires `onReset` on every filter change instead.
  const [currentOffset, setCurrentOffset] = useState(0)

  // Filter state — owned by useFilters<Filters> (PR 4 of the
  // 2026-06-09 architecture review). The hook collapses the
  // useState-per-field + per-field updater + clearAll + filter-
  // watching effect quartet shared with tasks-/agents-dashboard.tsx.
  // `onReset` preserves the v5.0.26 "filter changed -> page 1"
  // semantics that used to live in a dedicated useEffect.
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
  //   * composeSubject — user-typed or Suggest-populated root subject.
  //     Hidden + ignored when replying.
  //   * composeReplyParentId — non-empty means this is a reply to the
  //     named root message_id; the backend force-NULLs subject in that
  //     case, and we hide the subject input.
  //   * suggestLoading — guards the Suggest button so it can't double-fire.
  const [composeSubject, setComposeSubject] = useState("")
  const [composeReplyParentId, setComposeReplyParentId] = useState<string | null>(
    null,
  )
  const [suggestLoading, setSuggestLoading] = useState(false)

  // Participants drive the Compose recipient dropdown only.
  //
  // History (pre-feat/agent-select-dropdown):
  //   - The old code sourced from apiClient.getAgents(), which returns
  //     EVERY row including status='terminated' — leaking ghost agents
  //     into the From/To filters and Compose recipient.
  //   - PR #N introduced /api/messages/participants returning
  //     {live, tombstones}; Compose used `live` only, From/To filters
  //     concatenated `live + tombstones` so admins could grep history
  //     for purged agents.
  //
  // Now (feat/agent-select-dropdown):
  //   - The From/To filter dropdowns use the shared <AgentSelect>
  //     which reads live agents directly from the data-store. They no
  //     longer surface tombstones; see the comment block on the
  //     filter <AgentSelect>s below for the tradeoff and follow-up.
  //   - The Compose recipient still uses /api/messages/participants
  //     because it needs the BROADCAST option ("*") which is NOT an
  //     agent and is outside <AgentSelect>'s contract. The hardcoded
  //     "admin" entry mirrors data-store::shouldDisplayAgent so the
  //     compose UX matches the rest of the dashboard.
  const [liveParticipants, setLiveParticipants] = useState<
    { agent_id: string; status?: string }[]
  >([])

  const loadParticipants = async () => {
    try {
      const t = await adminToken()
      const data = await callMessages("POST", "/participants", { token: t })
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
  // fields are dropped so the backend doesn't have to special-case
  // them; ``read`` is converted from its tri-state string form
  // ("" | "true" | "false") into the real boolean the API wants.
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

  // v5.0.31: paginated listing fetch — was a bespoke
  // ``callMessages POST query`` flow with its own
  // useState/loading/error plumbing; now owned by ``usePagedQuery<T>``
  // (PR 5 of the 2026-06-09 architecture review). The hook handles
  // POST body construction (``{token, limit, offset, ...filters}``),
  // AbortController cancellation on filter/cursor change, and
  // surfaces ``data`` / ``total`` / ``loading`` / ``error`` /
  // ``refresh`` directly. ``adminToken`` is an async producer so the
  // tokens-fetch is deferred until the first request and re-runs on
  // reload.
  const {
    data: messages,
    total,
    loading,
    error: queryError,
    refresh: refreshQuery,
  } = usePagedQuery<Message>({
    endpoint: "/messages/query",
    filters: queryFilters,
    limit: PAGE_SIZE,
    offset: currentOffset,
    token: adminToken,
  })

  // Surface either an action error (compose / mark / delete) OR the
  // hook's query error — whichever is freshest. The UI just wants
  // the most-recent failure string.
  const error: string | null =
    actionError ?? (queryError ? queryError.message : null)

  // Wrapper that also re-pulls participants + clears selection. The
  // hook owns the actual listing fetch; this wrapper preserves the
  // pre-migration ``refresh()`` semantics where every refresh also
  // re-syncs tombstones and drops the selection set.
  const refresh = useCallback(() => {
    refreshQuery()
    setSelectedIds(new Set())
    void loadParticipants()
  }, [refreshQuery])

  // Detail modal — opened by clicking a row's content area (not the
  // checkbox / per-row action cells, which stopPropagation). Live-
  // lookup useDialog (Candidate D, 2026-06-02) stores only the
  // message_id and asks the selector for the current row on every
  // render — so when the local messages list is reloaded (e.g. after
  // a mark-read PATCH) the open dialog re-renders with the fresh
  // row instead of a snapshot from when it was opened. Declared
  // after ``usePagedQuery`` so the selector closure captures the
  // hook-owned ``messages`` array.
  const messageSelector = useCallback(
    (id: string | null) =>
      id ? messages.find((m) => m.message_id === id) ?? null : null,
    [messages],
  )
  const detailDialog = useDialog<Message>(messageSelector)

  // Deleted-while-open: if the row is removed from the list (delete
  // from any source — this tab, another tab, server-side cleanup),
  // the selector returns null. Auto-close so the user isn't stuck
  // on an empty modal. Explicit detailDialog.close() in deleteOne is
  // redundant but kept for code clarity.
  useEffect(() => {
    if (detailDialog.isOpen && detailDialog.data === null) detailDialog.close()
  }, [detailDialog.isOpen, detailDialog.data, detailDialog.close])

  // v5.0.24 polish: human-readable label for a parent message id.
  // Used by the reply chip + the in-table reply marker so the user
  // sees "reply to: Build debug help" instead of the opaque
  // "reply to: msg_a04d4d5666e5c19d".
  //
  // Lookup order:
  //   1. If we have the parent in the current page (messages map),
  //      return its subject; else first 40 chars of its content.
  //   2. Otherwise fall back to the message_id — the page just
  //      didn't load that far back, but the link is still valid.
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
    setActionError(null)
    try {
      const t = await adminToken()
      // BROADCAST sentinel maps to recipient_id="*" on the backend.
      const recipient =
        composeRecipient === BROADCAST ? "*" : composeRecipient
      // v5.0.22 — wire the new fields through. Reply mode pins
      // parent_message_id and lets the backend force-NULL the subject;
      // otherwise we either pass the typed subject (verbatim) or omit
      // the key entirely so the backend can pick suggest_subject /
      // truncated body.
      const body: Record<string, unknown> = {
        token: t,
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
      await refresh()
    } catch (e: any) {
      setActionError(e.message ?? String(e))
    } finally {
      setComposing(false)
    }
  }

  // v5.0.22: ask the backend (which delegates to Ollama if
  // AGENT_MCP_SUBJECT_MODEL is configured) to propose a subject from
  // the current compose body. Returns {subject: string | null}.
  //
  // v5.0.24 polish: when the response is null (Ollama unconfigured
  // OR helper returned empty), surface a transient hint so the user
  // knows the silence is intentional, not a hang. The hint clears on
  // the next Suggest attempt or when the user types into the field.
  const [suggestHint, setSuggestHint] = useState<string | null>(null)
  const suggestSubject = async () => {
    if (!composeContent.trim()) return
    setSuggestLoading(true)
    setActionError(null)
    setSuggestHint(null)
    try {
      const t = await adminToken()
      const data = await callMessages("POST", "/suggest-subject", {
        token: t,
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
    } catch (e: any) {
      // Soft-fail — the user can still type a subject manually.
      setActionError(e.message ?? String(e))
    } finally {
      setSuggestLoading(false)
    }
  }

  // Open the compose form pre-wired for a reply to the given message.
  // Mirrors how an email client's "Reply" button works: prefill the
  // recipient (other party of the parent thread), pin parent_message_id,
  // hide the subject input.
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
    try {
      const t = await adminToken()
      const nextRead = !(m.read === 1 || m.read === true)
      await callMessages("PATCH", `/${m.message_id}`, {
        token: t,
        read: nextRead,
      })
      // Live-lookup useDialog (Candidate D, 2026-06-02): no explicit
      // dialog-sync hack needed. The dialog reads the row live from
      // `messages`; refreshing the list propagates the new read state
      // into the open modal automatically.
      await refresh()
    } catch (e: any) {
      setActionError(e.message ?? String(e))
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
    setActionError(null)
    try {
      const t = await adminToken()
      await Promise.all(
        Array.from(selectedIds).map((id) =>
          callMessages("PATCH", `/${id}`, { token: t, read })
        )
      )
      await refresh()
    } catch (e: any) {
      setActionError(e.message ?? String(e))
    }
  }

  const bulkDelete = async () => {
    if (selectedIds.size === 0) return
    setActionError(null)
    try {
      const t = await adminToken()
      await Promise.all(
        Array.from(selectedIds).map((id) =>
          callMessages("DELETE", `/${id}`, { token: t })
        )
      )
      await refresh()
    } catch (e: any) {
      setActionError(e.message ?? String(e))
    }
  }

  // v5.0.26 pagination handlers. Disabled-state in the JSX guards
  // against overshoot, so these don't need to clamp defensively beyond
  // the Math.max on Newer (which prevents a negative offset when
  // PAGE_SIZE changes).
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

  const deleteOne = async (m: Message) => {
    setActionError(null)
    try {
      const t = await adminToken()
      await callMessages("DELETE", `/${m.message_id}`, { token: t })
      // Close the detail modal if it was showing the deleted row, so
      // we don't strand the user on a record that no longer exists.
      if (detailDialog.data?.message_id === m.message_id) {
        detailDialog.close()
      }
      await refresh()
    } catch (e: any) {
      setActionError(e.message ?? String(e))
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
            {/* v5.0.22: Subject input + Suggest button. Hidden when
                replying — replies always have subject = NULL per the
                schema contract. */}
            {composeReplyParentId ? (
              <div className="rounded-md border border-muted bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                {/* v5.0.24 polish: show the parent's subject (or a
                    content snippet) instead of the opaque
                    message_id. */}
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
                      // v5.0.24 polish: typing into the field clears
                      // any stale "no suggestion" hint.
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
          {/* CC-22 audit 2026-06-02: filter grid stepping was
              `grid-cols-1 md:grid-cols-6` — 6 cols on tablet (768)
              squished SelectTriggers to where the value text
              ("priority", "any sender") didn't fit. Now stepped
              1 → 2 → 3 → 6 so each step keeps comfortable widths. */}
          <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-6">
            {/*
              Migrated 2026-06-04 (feat/agent-select-dropdown): the
              From/To filter dropdowns now share <AgentSelect> with
              every other agent-input site in the dashboard.
              noneLabel="— Any —" because filter semantics differ from
              task assignment ("Unassigned"): an empty filter means
              "no filter".

              Tradeoff acknowledged in the PR body: the previous
              implementation sourced from `filterOptions` which
              appended `tombstones` (sender_id / recipient_id strings
              starting with "[deleted-...") so an admin could still
              grep history for purged agents. <AgentSelect> sources
              live agents only — per the locked design decision in
              the prancy-napping-pie plan. If the lost-tombstone-
              search affordance matters in practice, a follow-up PR
              should add a parallel "Tombstones" search box or extend
              <AgentSelect> with an explicit `extraItems` prop. For
              now, consistency wins.
            */}
            <AgentSelect
              value={filters.from || null}
              onChange={(v) => setFilter("from", v ?? "")}
              noneLabel="— Any —"
              placeholder="from"
            />
            <AgentSelect
              value={filters.to || null}
              onChange={(v) => setFilter("to", v ?? "")}
              noneLabel="— Any —"
              placeholder="to"
            />
            {/* Retain the underlying ALL sentinel constant for the
                non-agent filter dropdowns below (type / priority /
                read?). Those have their own enums and are not
                migration targets for <AgentSelect>. */}
            <Select
              value={filters.type || ALL}
              onValueChange={(v) => setFilter("type", v === ALL ? "" : v)}
            >
              <SelectTrigger><SelectValue placeholder="type" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>all types</SelectItem>
                {MESSAGE_TYPES.map((t) => (<SelectItem key={t} value={t}>{t}</SelectItem>))}
              </SelectContent>
            </Select>
            <Select
              value={filters.priority || ALL}
              onValueChange={(v) => setFilter("priority", v === ALL ? "" : v)}
            >
              <SelectTrigger><SelectValue placeholder="priority" /></SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>any priority</SelectItem>
                {PRIORITIES.map((p) => (<SelectItem key={p} value={p}>{p}</SelectItem>))}
              </SelectContent>
            </Select>
            <Select
              value={filters.read || ALL}
              onValueChange={(v) => setFilter("read", v === ALL ? "" : (v as "true" | "false"))}
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
              onChange={(e) => setFilter("q", e.target.value)}
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
          {/* CC-3/CC-6/CC-7/CC-20 audit 2026-06-02: Skeleton during
              the initial load, shared EmptyState body when empty (was
              just a blank table region under "0 messages"), and a
              mobile <MessagesMobileList> sibling for narrow viewports
              where the 9-column table overflows horizontally. Also
              fixed the desktop column labels — "Pri" / "R" were
              truncated abbreviations; now full "Priority" / "Read?"
              at sm:+ and the columns dropped from the mobile view. */}
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
                      {/* v5.0.22: subject column between To and Type so
                          the most-scanned thread label sits next to
                          the participants. */}
                      <TableHead>Subject</TableHead>
                      <TableHead>Type</TableHead>
                      <TableHead>Priority</TableHead>
                      <TableHead>Read?</TableHead>
                      <TableHead>Content</TableHead>
                      <TableHead className="w-8"></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {messages.map((m) => {
                      const checked = selectedIds.has(m.message_id)
                      // v5.0.22: rows whose parent_message_id is non-null
                      // are replies. Visual cue = subtle left border +
                      // indent + "↳ reply to: <parent_id>" prefix in
                      // the Subject column.
                      const isReply = !!m.parent_message_id
                      return (
                        <TableRow
                          key={m.message_id}
                          className={
                            "cursor-pointer" +
                            (isReply ? " border-l-2 border-l-muted-foreground/30" : "")
                          }
                          onClick={() => detailDialog.open(m.message_id)}
                        >
                          <TableCell onClick={(e) => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              aria-label={`select message ${m.message_id}`}
                              checked={checked}
                              onChange={() => toggleOne(m.message_id)}
                            />
                          </TableCell>
                          <TableCell className="text-xs font-mono tabular-nums">
                            {m.timestamp.slice(0, 19)}
                          </TableCell>
                          <TableCell><Badge variant="outline">{m.sender_id}</Badge></TableCell>
                          <TableCell><Badge variant="outline">{m.recipient_id}</Badge></TableCell>
                          <TableCell className="text-xs max-w-[200px] truncate">
                            {m.subject ? (
                              m.subject
                            ) : isReply ? (
                              // v5.0.24 polish: human-readable parent
                              // label instead of the opaque message_id.
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
                          <TableCell>
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
              </div>
              {/* Mobile card-list (CC-7) */}
              <div className="block sm:hidden -m-6">
                <MessagesMobileList
                  messages={messages}
                  selectedIds={selectedIds}
                  toggleOne={toggleOne}
                  openDetail={(m) => detailDialog.open(m.message_id)}
                  deleteOne={deleteOne}
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
          {/* v5.0.26: pagination footer — desktop only. Hidden on
              mobile because <MessagesMobileList> renders its own
              stacked-vertical variant (the desktop flex row doesn't
              fit narrow viewports comfortably). */}
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

      <Dialog
        open={detailDialog.isOpen}
        onOpenChange={(open) => {
          if (!open) detailDialog.close()
        }}
      >
        <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-2xl">
          {/* Local alias keeps the rest of the JSX untouched — every
              reference to `detailMessage` below reads the hook's data. */}
          {detailDialog.data && (() => { const detailMessage = detailDialog.data; return (
            <>
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2">
                  <MessageSquare className="h-5 w-5 text-primary" />
                  Message detail
                </DialogTitle>
                <DialogDescription>
                  {new Date(detailMessage.timestamp).toLocaleString()} ·{" "}
                  {relativeTime(detailMessage.timestamp)}
                </DialogDescription>
              </DialogHeader>

              <div className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
                <div>
                  <div className="text-xs text-muted-foreground">Sender</div>
                  <div className="font-mono break-all">
                    {detailMessage.sender_id}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">Recipient</div>
                  <div className="font-mono break-all">
                    {detailMessage.recipient_id}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">Type</div>
                  <div>
                    <Badge variant="outline">{detailMessage.message_type}</Badge>
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">Priority</div>
                  <div>
                    <Badge variant="outline">{detailMessage.priority}</Badge>
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">Delivered</div>
                  <div>
                    {detailMessage.delivered === 1 ||
                    detailMessage.delivered === true ? (
                      <Badge variant="secondary">✓ delivered</Badge>
                    ) : (
                      <Badge variant="outline">✗ pending</Badge>
                    )}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">Read</div>
                  <div>
                    {detailMessage.read === 1 ||
                    detailMessage.read === true ? (
                      <Badge variant="secondary">✓ read</Badge>
                    ) : (
                      <Badge variant="outline">✗ unread</Badge>
                    )}
                  </div>
                </div>
              </div>

              <div className="space-y-1">
                <div className="text-xs text-muted-foreground">Content</div>
                <pre className="whitespace-pre-wrap break-words rounded-md border bg-muted/50 p-3 font-mono text-xs max-h-[40vh] overflow-auto">
                  {detailMessage.message_content}
                </pre>
              </div>

              <div className="text-[10px] font-mono text-muted-foreground break-all">
                Message ID: {detailMessage.message_id}
              </div>

              <DialogFooter>
                {/* v5.0.22: Reply opens the compose form pre-wired
                    with parent_message_id pinned to this row. */}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    openReply(detailMessage)
                    detailDialog.close()
                  }}
                >
                  <Send className="h-4 w-4 mr-1" />
                  Reply
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => toggleRead(detailMessage)}
                >
                  {detailMessage.read === 1 || detailMessage.read === true ? (
                    <>
                      <Mail className="h-4 w-4 mr-1" />
                      Mark unread
                    </>
                  ) : (
                    <>
                      <MailOpen className="h-4 w-4 mr-1" />
                      Mark read
                    </>
                  )}
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => deleteOne(detailMessage)}
                >
                  <Trash2 className="h-4 w-4 mr-1" />
                  Delete
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => detailDialog.close()}
                >
                  Close
                </Button>
              </DialogFooter>
            </>
          ); })()}
        </DialogContent>
      </Dialog>
    </div>
  )
}
