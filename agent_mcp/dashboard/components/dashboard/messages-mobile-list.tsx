"use client"

import * as React from "react"
import { Trash2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  priorityBadgeClass,
  messageTypeBadgeClass,
} from "@/components/dashboard/shared/message-badges"

/**
 * Mobile card-list rendering of the messages table (CC-7 audit
 * 2026-06-02).
 *
 * Desktop has 9 columns (checkbox / Time / From / To / Type /
 * Priority / Read? / Content / delete). At 375 px this overflows
 * horizontally and only the first 6 columns fit even with the table
 * wrapper's `overflow-x-auto` — the audit screenshot confirms
 * Content + delete are off-screen.
 *
 * Mobile renders each message as a card: checkbox + From → To header,
 * Type / Priority badges, content snippet, timestamp + delete row.
 * Stays full-bleed inside the CardContent (negative margin in the
 * parent) so the row dividers run edge-to-edge.
 */

interface MessageRow {
  message_id: string
  sender_id: string
  recipient_id: string
  message_content: string
  message_type: string
  priority: string
  timestamp: string
  delivered: number | boolean
  read: number | boolean
  // v5.0.22 message threads + subjects.
  subject: string | null
  // True when `subject` is an auto-generated preview (Phase 1/2), not a
  // real subject — rendered as a muted "auto" placeholder.
  subject_is_placeholder?: boolean
  parent_message_id: string | null
}

interface MessagesMobileListProps {
  messages: MessageRow[]
  selectedIds: Set<string>
  toggleOne: (id: string) => void
  openDetail: (m: MessageRow) => void
  deleteOne: (m: MessageRow) => void
  // v5.0.24 polish: parent-id → human-readable label resolver.
  // Falls back to the message_id if the parent isn't loaded.
  // Optional so older callers (none currently in-tree) still compile.
  labelForParent?: (parentId: string | null) => string
  // v5.0.26 pagination — wired by the parent
  // (messages-dashboard.tsx) so the mobile list can render the same
  // « Newest / Newer / Older / Oldest » footer the desktop does, but
  // stacked-vertical for narrow viewports. Optional so older callers
  // (none currently in-tree) still compile.
  currentOffset?: number
  total?: number
  pageSize?: number
  onNewest?: () => void
  onNewer?: () => void
  onOlder?: () => void
  onOldest?: () => void
}

export function MessagesMobileList({
  messages,
  selectedIds,
  toggleOne,
  openDetail,
  deleteOne,
  labelForParent,
  currentOffset,
  total,
  pageSize,
  onNewest,
  onNewer,
  onOlder,
  onOldest,
}: MessagesMobileListProps): React.ReactElement {
  // v5.0.26 pagination footer derived state. Only render the footer
  // when the parent wired the props (back-compat) and the dataset has
  // something to paginate (total > 0). Disabled-state mirrors the
  // desktop variant in messages-dashboard.tsx.
  const showPagination =
    typeof currentOffset === "number" &&
    typeof total === "number" &&
    typeof pageSize === "number" &&
    total > 0 &&
    !!onNewest && !!onNewer && !!onOlder && !!onOldest
  const onFirstPage =
    showPagination && (currentOffset as number) === 0
  const onLastPage =
    showPagination &&
    (currentOffset as number) + (pageSize as number) >= (total as number)
  const rangeStart = showPagination ? (currentOffset as number) + 1 : 0
  const rangeEnd = showPagination
    ? Math.min((currentOffset as number) + (pageSize as number), total as number)
    : 0

  return (
    <>
      <ul role="list" className="divide-y divide-border">
      {messages.map((m) => {
        const isRead = m.read === 1 || m.read === true
        // v5.0.22: reply rows render with a left border + indent so the
        // mobile list mirrors the desktop visual treatment.
        const isReply = !!m.parent_message_id
        return (
          <li
            key={m.message_id}
            onClick={() => openDetail(m)}
            className={
              "px-4 py-3 hover:bg-muted/30 active:bg-muted/50 transition-colors duration-150 cursor-pointer" +
              (isReply ? " border-l-2 border-l-muted-foreground/30 pl-6" : "")
            }
          >
            <div className="flex items-start gap-3">
              <input
                type="checkbox"
                aria-label={`select message ${m.message_id}`}
                checked={selectedIds.has(m.message_id)}
                onChange={() => toggleOne(m.message_id)}
                onClick={(e) => e.stopPropagation()}
                className="mt-1 h-4 w-4 accent-primary"
              />
              <div className="min-w-0 flex-1">
                {/* Show the FULL sender/recipient ids: the header wraps
                    (flex-wrap) so a long pair drops the recipient onto its
                    own line, and each id badge grows to the row width and
                    breaks within it (break-all) rather than truncating to
                    "pikvm-nixos…". Reading who↔who no longer needs a
                    hover/long-press. */}
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
                  <Badge
                    variant="outline"
                    className="px-1.5 py-0 text-[10px] max-w-full"
                    title={m.sender_id}
                  >
                    <span className="break-all">{m.sender_id}</span>
                  </Badge>
                  <span aria-hidden className="shrink-0">→</span>
                  <Badge
                    variant="outline"
                    className="px-1.5 py-0 text-[10px] max-w-full"
                    title={m.recipient_id}
                  >
                    <span className="break-all">{m.recipient_id}</span>
                  </Badge>
                  {!isRead && (
                    <span
                      aria-label="unread"
                      className="ml-auto h-2 w-2 shrink-0 rounded-full bg-primary"
                    />
                  )}
                </div>
                {/* v5.0.22: surface subject (root) or reply marker
                    (reply) on its own line above the body. */}
                {m.subject && m.subject_is_placeholder ? (
                  // Placeholder: no subject set → auto-preview of the body
                  // (Phase 1). Muted + italic with an "auto" tag so it reads
                  // as a stub; the backfill sweep titles it later (Phase 2).
                  <p
                    className="text-sm mt-1 italic text-muted-foreground truncate"
                    title="No subject set — auto-preview of the message. A generated subject will fill in shortly."
                  >
                    {m.subject}
                    <span className="ml-1 not-italic text-[10px] font-medium uppercase tracking-wide text-muted-foreground/60">
                      auto
                    </span>
                  </p>
                ) : m.subject ? (
                  <p
                    className={
                      "text-sm mt-1 font-medium truncate " +
                      (isRead ? "text-muted-foreground" : "text-foreground")
                    }
                  >
                    {m.subject}
                  </p>
                ) : isReply ? (
                  // v5.0.24 polish: human-readable parent label.
                  <p className="text-[11px] mt-1 text-muted-foreground">
                    ↳ reply to:{" "}
                    <span className="text-foreground">
                      {labelForParent
                        ? labelForParent(m.parent_message_id)
                        : m.parent_message_id}
                    </span>
                  </p>
                ) : null}
                <p
                  className={
                    "text-sm mt-1 line-clamp-2 break-words " +
                    (isRead ? "text-muted-foreground" : "text-foreground")
                  }
                  title={m.message_content}
                >
                  {m.message_content}
                </p>
                <div className="flex flex-wrap items-center gap-1.5 mt-2 text-[11px] text-muted-foreground">
                  <span className="font-mono tabular-nums">
                    {m.timestamp.slice(0, 19)}
                  </span>
                  <Badge
                    variant="outline"
                    className={"px-1.5 py-0 text-[10px] " + messageTypeBadgeClass(m.message_type)}
                  >
                    {m.message_type}
                  </Badge>
                  {m.priority && (
                    <Badge
                      variant="outline"
                      className={"px-1.5 py-0 text-[10px] " + priorityBadgeClass(m.priority)}
                    >
                      {m.priority}
                    </Badge>
                  )}
                </div>
              </div>
              <Button
                variant="ghost"
                size="sm"
                aria-label="delete message"
                title="Delete message"
                className="h-9 w-9 p-0 -mr-1 text-destructive hover:text-destructive hover:bg-destructive/10"
                onClick={(e) => { e.stopPropagation(); deleteOne(m) }}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          </li>
        )
      })}
      </ul>
      {/* v5.0.26: mobile pagination footer. Rendered INSIDE the
          parent's `-m-6` full-bleed container (so the row divider
          line continues edge-to-edge above), but its own node is
          wrapped in `px-6 py-4` counter-padding to re-align the
          visible label + buttons back to the card boundary.
          Stacked-vertical: label row above a 4-column grid of
          buttons so the labels stay readable at 375 px. */}
      {showPagination && (
        <div className="px-6 py-4 border-t bg-background">
          <div className="text-[11px] text-muted-foreground tabular-nums text-center mb-2">
            Showing {rangeStart}–{rangeEnd} of {total}
          </div>
          <div className="grid grid-cols-4 gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onNewest}
              disabled={onFirstPage}
              aria-label="jump to newest page"
            >
              « Newest
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onNewer}
              disabled={onFirstPage}
            >
              Newer
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onOlder}
              disabled={onLastPage}
            >
              Older
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onOldest}
              disabled={onLastPage}
              aria-label="jump to oldest page"
            >
              Oldest »
            </Button>
          </div>
        </div>
      )}
    </>
  )
}
