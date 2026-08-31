"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  MessageSquare,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { type Message } from "@/lib/api"
import { useDialog } from "@/hooks/use-dialog"
import { useFilters } from "@/hooks/use-filters"
import { useMessagesQuery } from "@/lib/queries/messages"
import { useServerStore } from "@/lib/stores/server-store"
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { MessageMobileCard } from "@/components/dashboard/messages-mobile-list"
import { ViewMessageModal } from "@/components/dashboard/messages/view-message-modal"
import { DeleteConfirmModal } from "@/components/dashboard/modals/delete-confirm-modal"
import { ComposeMessageModal } from "@/components/dashboard/messages/compose-message-modal"
import { MessageDeletePreview } from "@/components/dashboard/messages/message-delete-preview"
import { MessagesPagination } from "@/components/dashboard/messages/messages-pagination"
import { useMessagesColumns } from "@/components/dashboard/messages/use-messages-columns"
import {
  ALL,
  MESSAGE_TYPES,
  PRIORITIES,
  callMessages,
} from "@/components/dashboard/messages/messages-api"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { DataTablePage } from "@/components/dashboard/shared/data-table-page"
import type { EmptyStateProps } from "@/components/dashboard/shared/empty-state"
import type { StatsCardProps } from "@/components/dashboard/shared/stats-card"
import { FilterField } from "@/components/dashboard/shared/filter-field"

interface Filters {
  from: string
  to: string
  type: string
  priority: string
  read: "" | "true" | "false"
  q: string
}

// v5.0.26: pagination footer on the messages list. Per-page size stays
// at 100 (Dennis explicitly does not want bigger pages — see plan
// prancy-napping-pie.md). The footer adds « Newest / Newer / Older /
// Oldest » cursor buttons + a "Showing N–M of T" range label so admins
// can reach messages past the first 100 rows. Component state only —
// no URL state, matches the existing filter behavior.
const PAGE_SIZE = 100

// Stable empty page for the no-data path (first load / disconnected /
// query disabled) — a fresh object each render would defeat reference
// equality on `messages` and re-run the memoised filtered/stats passes.
const EMPTY_PAGE = { messages: [] as Message[], total: 0 }

export function MessagesDashboard() {
  // Server-online indicator (matches Agents/Tasks/Memories header).
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find((s) => s.id === activeServerId)

  // v5.0.26: pagination cursor. Total comes from the query. Declared
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

  // Compose modal state. `replyTo` seeds the compose form for a reply
  // (null for a fresh compose); the modal owns the draft fields.
  const [composeOpen, setComposeOpen] = useState(false)
  const [replyTo, setReplyTo] = useState<Message | null>(null)

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

  // W6-followup F3: the paginated listing fetch now rides the shared
  // TanStack Query client via ``useMessagesQuery`` (see
  // ``lib/queries/messages.ts``), mirroring the tasks migration. The
  // query is keyed ``['messages', project, {filters, limit, offset}]``
  // and owns the loading/error/lastFetch/refetch state machine plus the
  // PF-3 SSE-gated background poll. Live updates arrive via the single
  // ``invalidateMessages()`` SSE choke point in
  // ``lib/mcp-notifications.ts`` — so the pre-migration 60s
  // ``setInterval`` and the ``mcp:resources-updated`` window listener are
  // both retired here. The debounced invalidation refetches the mounted
  // page IN PLACE at the current offset/filters (``keepPreviousData``
  // holds the rows on screen), so the operator's page/scroll are
  // preserved exactly as the old in-place refetch did — and, because a
  // background invalidation goes through the query (not the ``refresh``
  // wrapper below), it does NOT wipe the row selection either.
  const query = useMessagesQuery(queryFilters, PAGE_SIZE, currentOffset)
  const page = query.data ?? EMPTY_PAGE
  const messages = page.messages
  const total = page.total
  // `loading`: initial load only (no cached page yet) — drives the
  // skeleton. A background/SSE/page-step refetch keeps the current rows.
  const loading = query.isLoading
  // `refreshing`: any fetch in flight — drives the header spinner
  // (preserves the pre-migration "spins on manual Refresh" feel).
  const refreshing = query.isFetching
  // React Query exposes a real `Error`; the toast + scaffold want the
  // legacy `Error | null` / `string | null` shapes.
  const queryError: Error | null = query.error
    ? (query.error as Error)
    : null
  // `dataUpdatedAt` is 0 until the first success; the header checks `> 0`.
  const lastFetch = query.dataUpdatedAt

  // Surface the query error via the shared toast (matches
  // Agents/Tasks/Memories — no in-page red banner). `queryError` is ALSO
  // handed to <DataTablePage>, which degrades to an inline stale notice
  // whenever rows are in hand and only takes over the page on an empty
  // first load — so the toast announces the blip and the rows the
  // operator is reading stay put.
  useEffect(() => {
    if (queryError) toastError(queryError, "Failed to load messages")
  }, [queryError])

  // Manual-refresh wrapper: `refetch` is referentially stable (React
  // Query guarantees it), so this keeps a stable identity. It ALSO clears
  // the row selection so a manual Refresh (or a post-mutation refresh)
  // doesn't leave stale rows selected.
  const { refetch } = query
  const refresh = useCallback(() => {
    void refetch()
    setSelectedIds(new Set())
  }, [refetch])

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
  //
  // exhaustive-deps disabled for this block: useDialog returns a fresh
  // object each render, so we depend on its stable fields
  // (.isOpen/.data/.close) rather than the whole object. Listing the
  // object would re-run every render with no behavioural gain.
  /* eslint-disable react-hooks/exhaustive-deps */
  useEffect(() => {
    if (detailDialog.isOpen && detailDialog.data === null) detailDialog.close()
  }, [detailDialog.isOpen, detailDialog.data, detailDialog.close])
  useEffect(() => {
    if (deleteDialog.isOpen && deleteDialog.data === null) deleteDialog.close()
  }, [deleteDialog.isOpen, deleteDialog.data, deleteDialog.close])
  /* eslint-enable react-hooks/exhaustive-deps */

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

  // Open the compose modal pre-wired for a reply to the given message.
  // The compose modal owns the reply-seeding logic (reply AS the parent's
  // recipient, back TO its sender); here we just hand it the parent and
  // open it.
  const openReply = useCallback((parent: Message) => {
    setReplyTo(parent)
    setComposeOpen(true)
  }, [])

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

  const toggleOne = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const allVisibleSelected =
    messages.length > 0 && selectedIds.size === messages.length

  const toggleAllVisible = useCallback(() => {
    setSelectedIds((prev) =>
      prev.size === messages.length && messages.length > 0
        ? new Set()
        : new Set(messages.map((m) => m.message_id)),
    )
  }, [messages])

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
  // card. Extracted to `useMessagesColumns` (Wave 5, mirrors
  // `useAgentColumns`).
  const columns = useMessagesColumns({
    selectedIds,
    allVisibleSelected,
    onToggleAll: toggleAllVisible,
    onToggleOne: toggleOne,
    onDelete: (id) => deleteDialog.open(id),
    labelForParent,
  })

  // Everything between the stats strip and the table card. The scaffold
  // exposes ONE slot there (`filterBar`); the filter controls + the
  // selection toolbar both belong in that band. (The compose form used
  // to live here as an inline card; Wave 5 moves it into the shared
  // <FormDialog>-based <ComposeMessageModal>.)
  const filterBar = (
    <div className="w-full space-y-4 sm:space-y-6">
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
      error={queryError?.message ?? null}
      header={{
        title: "Messages",
        subtitle: "Inspect and route inter-agent messages",
        serverName: activeServer?.name,
        lastUpdated: lastFetch > 0 ? lastFetch : undefined,
        onRefresh: refresh,
        refreshing: refreshing,
        actions: (
          <Button
            size="sm"
            onClick={() => {
              setReplyTo(null)
              setComposeOpen(true)
            }}
          >
            <Plus className="h-4 w-4 mr-1" />
            New Message
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

      {/* Compose modal — shared <FormDialog> + useAsyncSubmit shell
          (Wave 5). Opened fresh from the header "New Message" button, or
          pre-wired for a reply via openReply(). */}
      <ComposeMessageModal
        open={composeOpen}
        onOpenChange={setComposeOpen}
        parent={replyTo}
        labelForParent={labelForParent}
        onSent={refresh}
      />

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
