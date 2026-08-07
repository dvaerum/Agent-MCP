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
 * Mobile card rendering of a single message row (CC-7 audit 2026-06-02).
 *
 * Desktop has 10 columns (checkbox / Time / From / To / Subject / Type /
 * Priority / Read? / Content / delete). At 375 px this overflows
 * horizontally and only the first 6 columns fit even with the table
 * wrapper's `overflow-x-auto` — the audit screenshot confirms
 * Content + delete are off-screen.
 *
 * Mobile renders each message as a card: checkbox + From → To header,
 * subject / reply marker, content snippet, timestamp + type/priority
 * badges, delete button.
 *
 * This is a *single card* (`<li>`); the `<ul>` wrapper is provided by
 * <ResponsiveDataTable>'s `renderMobileCard` slot. Pre-foundation this
 * file exported a whole-list `<MessagesMobileList>` (plus its own copy
 * of the pagination footer) — the list shell now belongs to the shared
 * scaffold and the pagination footer to messages-dashboard.tsx, leaving
 * only the per-row markup here.
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

interface MessageMobileCardProps {
  message: MessageRow
  selected: boolean
  toggleOne: (id: string) => void
  openDetail: (m: MessageRow) => void
  deleteOne: (m: MessageRow) => void
  // v5.0.24 polish: parent-id → human-readable label resolver.
  // Falls back to the message_id if the parent isn't loaded.
  // Optional so older callers (none currently in-tree) still compile.
  labelForParent?: (parentId: string | null) => string
}

export function MessageMobileCard({
  message: m,
  selected,
  toggleOne,
  openDetail,
  deleteOne,
  labelForParent,
}: MessageMobileCardProps): React.ReactElement {
  const isRead = m.read === 1 || m.read === true
  // v5.0.22: reply rows render with a left border + indent so the
  // mobile list mirrors the desktop visual treatment.
  const isReply = !!m.parent_message_id
  return (
    <li
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
          checked={selected}
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
}
