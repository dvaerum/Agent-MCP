"use client"

import * as React from "react"
import { Trash2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

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
  parent_message_id: string | null
}

interface MessagesMobileListProps {
  messages: MessageRow[]
  selectedIds: Set<string>
  toggleOne: (id: string) => void
  openDetail: (m: MessageRow) => void
  deleteOne: (m: MessageRow) => void
}

export function MessagesMobileList({
  messages,
  selectedIds,
  toggleOne,
  openDetail,
  deleteOne,
}: MessagesMobileListProps): React.ReactElement {
  return (
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
                <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                  <Badge variant="outline" className="px-1.5 py-0 text-[10px]">
                    {m.sender_id}
                  </Badge>
                  <span aria-hidden>→</span>
                  <Badge variant="outline" className="px-1.5 py-0 text-[10px]">
                    {m.recipient_id}
                  </Badge>
                  {!isRead && (
                    <span
                      aria-label="unread"
                      className="ml-auto h-2 w-2 rounded-full bg-primary"
                    />
                  )}
                </div>
                {/* v5.0.22: surface subject (root) or reply marker
                    (reply) on its own line above the body. */}
                {m.subject ? (
                  <p
                    className={
                      "text-sm mt-1 font-medium truncate " +
                      (isRead ? "text-muted-foreground" : "text-foreground")
                    }
                  >
                    {m.subject}
                  </p>
                ) : isReply ? (
                  <p className="text-[11px] mt-1 text-muted-foreground">
                    ↳ reply to:{" "}
                    <span className="font-mono">{m.parent_message_id}</span>
                  </p>
                ) : null}
                <p
                  className={
                    "text-sm mt-1 line-clamp-2 " +
                    (isRead ? "text-muted-foreground" : "text-foreground")
                  }
                >
                  {m.message_content}
                </p>
                <div className="flex items-center gap-2 mt-2 text-[11px] text-muted-foreground">
                  <span className="font-mono tabular-nums">
                    {m.timestamp.slice(0, 19)}
                  </span>
                  <span aria-hidden>·</span>
                  <span>{m.message_type}</span>
                  {m.priority && m.priority !== "normal" && (
                    <>
                      <span aria-hidden>·</span>
                      <span>{m.priority}</span>
                    </>
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
  )
}
